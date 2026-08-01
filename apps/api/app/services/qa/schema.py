"""모델 출력 파싱·서버측 출처 검증.

모델 결과를 절대 그대로 저장하지 않는다. 각 claim의 sourceChunkIds가 이번 검색 청크의
부분집합이며 현재 문서 소속인지 확인하고, page/bbox는 저장된 source_refs에서 재구성한다.
수치 주장은 출처 청크 원문에 실제로 존재하는지 검증한다.
"""

import math
import re
from dataclasses import dataclass, field

from app.models.enums import QaClaimVerification
from app.services.qa.context import QaChunkRef
from app.services.qa.settings import (
    ANSWER_MAX_CHARS,
    CLAIM_TEXT_MAX_CHARS,
    MAX_CLAIMS,
    MAX_FOLLOWUPS,
)

# claim 안의 수치 토큰(콤마·소수·백분율 포함). 자릿수 경계로 검사해 50이 150에 매치되지
# 않게 한다.
_CLAIM_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
# 어절 토큰(길이 2 이상) — claim이 근거 청크에 실제로 어휘적으로 연결되는지 확인용
_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")
# 부정 극성 표지 — 어휘 중복만으로 "A는 X한다"의 반대인 "A는 X하지 않는다"가 통과하는
# 것을 막는다(의료 안전상 극성 뒤집힘이 가장 위험). 한국어 부정소 + 영어 부정어.
_NEGATION_MARKERS = ("않", "없", "아니", "못", " 안 ", " no ", " not ", "n't", "없이")


@dataclass
class VerifiedClaim:
    claim_index: int
    text: str
    verification_status: QaClaimVerification
    source_chunk_ids: list[str]
    source_refs: list[dict]


@dataclass
class VerifiedAnswer:
    answer: str
    answer_status: str  # completed|not_found|insufficient_evidence|conflicting_evidence
    claims: list[VerifiedClaim]
    followups: list[str] = field(default_factory=list)


def verify_claim_event(
    event: dict, lookup: dict[str, QaChunkRef], *, claim_index: int
) -> VerifiedClaim | None:
    """스트리밍 claim 이벤트 하나를 검증한다. 지원(supported)일 때만 반환, 아니면 None.

    4A와 동일한 검증(현재 검색 청크 부분집합·문서 소속·어휘 연결·수치 원문 존재)을 쓴다.
    출처(page/bbox)는 저장된 source_refs에서 재구성한다.
    """
    text = str(event.get("text") or "").strip()[:CLAIM_TEXT_MAX_CHARS]
    if not text:
        return None
    ids = _valid_ids(event.get("sourceChunkIds"), lookup)
    if not ids or not _numbers_present(text, ids, lookup) or not _lexically_grounded(
        text, ids, lookup
    ):
        return None  # unsupported → 스트림으로 내보내지 않는다
    return VerifiedClaim(
        claim_index=claim_index,
        text=text,
        verification_status=QaClaimVerification.SUPPORTED,
        source_chunk_ids=ids,
        source_refs=_refs_for(ids, lookup),
    )


def _valid_ids(raw_ids, lookup: dict[str, QaChunkRef]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for rid in raw_ids or []:
        sid = str(rid)
        if sid in lookup and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _refs_for(ids: list[str], lookup: dict[str, QaChunkRef]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    for cid in ids:
        for ref in lookup[cid].source_refs:
            key = (ref.get("pageNumber"), ref.get("blockId"), tuple(ref.get("bbox") or []))
            if key not in seen:
                seen.add(key)
                out.append(ref)
    return out


def _numbers_present(claim_text: str, ids: list[str], lookup: dict[str, QaChunkRef]) -> bool:
    """claim의 모든 수치가 근거 청크 원문에 존재하는지(자릿수 경계 검사). 없으면 통과."""
    numbers = [m.group(0) for m in _CLAIM_NUMBER_RE.finditer(claim_text)]
    if not numbers:
        return True
    # 콤마를 제거해 "1,000"과 "1000"을 같게 본다
    haystack = "\n".join(lookup[c].text for c in ids).replace(",", "")
    for raw in numbers:
        core = raw.replace(",", "")
        # 자릿수 경계로 매치 — 50이 150·250에 매치되지 않게 한다(% 포함 형태도 처리)
        digits = core.rstrip("%")
        # 뒤에 숫자가 이어지면 매치 금지 — "50%"가 "500명"의 "50"에 매치되지 않게 한다.
        tail = r"%?(?!\d)" if core.endswith("%") else r"(?!\d)"
        pattern = r"(?<!\d)" + re.escape(digits) + tail
        if not re.search(pattern, haystack):
            return False
    return True


def _lexically_grounded(claim_text: str, ids: list[str], lookup: dict[str, QaChunkRef]) -> bool:
    """claim이 근거 청크에 어휘적으로 연결되는지 — 무관한 날조에 임의 chunk id를 붙인
    경우를 걸러낸다. 완전한 함의 검증은 아니지만(그건 검증 모델이 필요), 근거 청크와
    공유 토큰이 사실상 없는 주장을 supported로 저장하지 않는다."""
    claim_tokens = {t.lower() for t in _WORD_RE.findall(claim_text)}
    if not claim_tokens:
        return False
    haystack = "\n".join(lookup[c].text for c in ids).lower()
    hay_tokens = set(_WORD_RE.findall(haystack))
    shared = len(claim_tokens & hay_tokens)
    # claim 토큰의 1/3 이상(최소 1개)이 근거 청크에 나타나야 한다. 공유 어휘가 거의
    # 없는(=사실상 0) 무관한 날조를 걸러내는 게 목적이며, 완전한 함의 검증은 아니다.
    if not (shared >= max(1, math.ceil(len(claim_tokens) / 3)) or shared == len(claim_tokens)):
        return False
    return _polarity_consistent(claim_text, haystack)


def _polarity_consistent(claim_text: str, haystack: str) -> bool:
    """claim이 근거 청크에 없는 부정을 새로 도입하지 않는지 확인한다.

    어휘 중복만으로는 "A는 X한다"의 반대인 "A는 X하지 않는다"가 통과한다(핵심 토큰이
    거의 겹치므로). claim에 나타난 부정 표지가 근거 원문에도 있어야 supported로 인정한다.
    명시적 부정소만 잡는다. 증가↔감소 같은 반의어 뒤집힘은 함의 검증(NLI) 모델이
    있어야 하며 이번 스프린트 범위 밖 — 알려진 상한.
    """
    # 공백류(개행·탭 포함)를 단일 스페이스로 정규화 — " 안 " 같은 공백 포함 표지가
    # 줄바꿈 경계에서도 매치되게 한다.
    hay = " " + re.sub(r"\s+", " ", haystack) + " "
    claim = " " + re.sub(r"\s+", " ", claim_text.lower()) + " "
    for marker in _NEGATION_MARKERS:
        if marker in claim and marker not in hay:
            return False
    return True


def verify(
    model_output: dict, lookup: dict[str, QaChunkRef], *, had_results: bool
) -> VerifiedAnswer:
    """모델 출력 dict → 검증된 답변. 지원 claim만 남기고 최종 상태를 결정한다."""
    answer = str(model_output.get("answer") or "").strip()[:ANSWER_MAX_CHARS]
    model_status = str(model_output.get("answerStatus") or "").strip()

    verified: list[VerifiedClaim] = []
    seen_text: set[str] = set()
    idx = 0
    for raw in (model_output.get("claims") or [])[: MAX_CLAIMS * 2]:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()[:CLAIM_TEXT_MAX_CHARS]
        if not text or text in seen_text:
            continue
        ids = _valid_ids(raw.get("sourceChunkIds"), lookup)
        # 지원 조건: 유효 출처 있음 + 수치가 원문에 존재 + 근거 청크에 어휘적으로 연결됨
        if (
            not ids
            or not _numbers_present(text, ids, lookup)
            or not _lexically_grounded(text, ids, lookup)
        ):
            claim_status = QaClaimVerification.UNSUPPORTED
        else:
            claim_status = QaClaimVerification.SUPPORTED
        seen_text.add(text)
        verified.append(
            VerifiedClaim(
                claim_index=idx,
                text=text,
                verification_status=claim_status,
                source_chunk_ids=ids,
                source_refs=_refs_for(ids, lookup),
            )
        )
        idx += 1
        if len(verified) >= MAX_CLAIMS:
            break

    supported = [c for c in verified if c.verification_status == QaClaimVerification.SUPPORTED]

    # 최종 상태 결정 (모델 상태를 신뢰하지 않고 서버가 확정)
    if not had_results:
        status = "not_found"
    elif model_status == "conflicting_evidence" and len(supported) >= 2:
        # 모델이 상충을 명시하고 양쪽 출처가 유효할 때만 conflicting으로 인정
        status = "conflicting_evidence"
        for c in supported:
            c.verification_status = QaClaimVerification.CONFLICTING
    elif not supported:
        status = "insufficient_evidence"
    else:
        status = "completed"

    followups = [
        str(f).strip()
        for f in (model_output.get("followUpSuggestions") or [])
        if str(f).strip()
    ][:MAX_FOLLOWUPS]

    if status in ("not_found", "insufficient_evidence"):
        # 근거가 없으면 unsupported claim을 확정 사실처럼 저장하지 않는다
        verified = [c for c in verified if c.verification_status != QaClaimVerification.UNSUPPORTED]

    return VerifiedAnswer(
        answer=answer, answer_status=status, claims=verified, followups=followups
    )
