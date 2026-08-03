"""모델 출력 파싱·서버측 출처 검증.

모델 결과를 절대 그대로 저장하지 않는다. 각 claim의 sourceChunkIds가 이번 검색 청크의
부분집합이며 현재 문서 소속인지 확인하고, page/bbox는 저장된 source_refs에서 재구성한다.
수치 주장은 출처 청크 원문에 실제로 존재하는지 검증한다.
"""

import math
import re
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.models.enums import QaClaimVerification
from app.services.qa.context import QaChunkRef
from app.services.qa.settings import (
    ANSWER_MAX_CHARS,
    CLAIM_TEXT_MAX_CHARS,
    FOLLOWUP_MAX_CHARS,
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

logger = get_logger(__name__)

# 답변 산문 안의 인용 마커. 문서 본문에 흔한 대괄호(`[1]`, `[표 3]`)를 인용으로 오인하지
# 않도록 `c` 접두사를 요구한다. MAX_CLAIMS=20이라 두 자리면 충분하다.
_CITATION_RE = re.compile(r"\[c(\d{1,2})\]")

# 지원 claim 간 상충 감지에 필요한 최소 공유 주제 토큰 수. 서로 다른 근거 청크의 두
# 주장이 같은 대상을 다루면서(주제 어휘 충분히 겹침) 부정 극성만 반대일 때 상충으로 본다.
_CONFLICT_MIN_SHARED = 3


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


# 스트리밍 claim 거부 사유 코드 — 진단용(원문 비노출, 안전 코드만).
REJECT_EMPTY = "empty_text"
REJECT_NO_SOURCE = "no_valid_source"
REJECT_NUMBER_ABSENT = "number_not_in_source"
REJECT_NOT_GROUNDED = "not_lexically_grounded"


def classify_claim_event(
    event: dict, lookup: dict[str, QaChunkRef], *, claim_index: int
) -> tuple[VerifiedClaim | None, str | None]:
    """claim 이벤트를 검증한다. 반환: (지원 claim | None, 거부 사유 코드 | None).

    4A와 동일한 검증(현재 검색 청크 부분집합·문서 소속·어휘 연결·수치 원문 존재)을 쓴다.
    지원이면 (claim, None), 거부면 (None, 사유코드). 사유코드는 안전한 분류값이며 모델
    응답 원문을 담지 않는다.
    """
    text = str(event.get("text") or "").strip()[:CLAIM_TEXT_MAX_CHARS]
    if not text:
        return None, REJECT_EMPTY
    ids = _valid_ids(event.get("sourceChunkIds"), lookup)
    if not ids:
        return None, REJECT_NO_SOURCE
    if not _numbers_present(text, ids, lookup):
        return None, REJECT_NUMBER_ABSENT
    if not _lexically_grounded(text, ids, lookup):
        return None, REJECT_NOT_GROUNDED
    return VerifiedClaim(
        claim_index=claim_index,
        text=text,
        verification_status=QaClaimVerification.SUPPORTED,
        source_chunk_ids=ids,
        source_refs=_refs_for(ids, lookup),
    ), None


def verify_claim_event(
    event: dict, lookup: dict[str, QaChunkRef], *, claim_index: int
) -> VerifiedClaim | None:
    """스트리밍 claim 이벤트 하나를 검증한다. 지원(supported)일 때만 반환, 아니면 None."""
    vc, _reason = classify_claim_event(event, lookup, claim_index=claim_index)
    return vc  # unsupported → 스트림으로 내보내지 않는다


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


def _has_negation(text: str) -> bool:
    padded = " " + re.sub(r"\s+", " ", text.lower()) + " "
    return any(m in padded for m in _NEGATION_MARKERS)


def _subject_tokens(text: str) -> set[str]:
    """상충 비교용 주제 토큰 — 부정 표지를 담은 토큰은 제외한다(주제가 아니라 극성)."""
    tokens = {t.lower() for t in _WORD_RE.findall(text)}
    return {t for t in tokens if not any(m.strip() and m.strip() in t for m in _NEGATION_MARKERS)}


def claims_conflict(claim_texts: list[str]) -> bool:
    """서로 다른 근거의 두 주장이 같은 대상에 상반된 극성을 보이면 True(보수적).

    조건: 두 주장이 충분한 주제 어휘(_CONFLICT_MIN_SHARED개 이상)를 공유하면서, 한쪽만
    부정 극성을 담는 경우. 명시적 부정소 기반이라 증가↔감소 같은 반의어 뒤집힘은 잡지
    못하지만(알려진 상한), 무관한 다중 주장을 상충으로 오탐하지 않도록 보수적으로 잡는다.
    상충을 못 잡으면 오히려 상반 근거가 통일된 답처럼 노출되므로, 안전상 conflicting_evidence
    쪽으로 기운다(거짓 상충은 사용자에게 '출처 확인'을 유도할 뿐 사실을 날조하지 않는다).
    """
    n = len(claim_texts)
    if n < 2:
        return False
    negs = [_has_negation(t) for t in claim_texts]
    subjects = [_subject_tokens(t) for t in claim_texts]
    for i in range(n):
        for j in range(i + 1, n):
            if negs[i] == negs[j]:
                continue  # 극성 차이가 없으면 상충으로 보지 않는다
            if len(subjects[i] & subjects[j]) >= _CONFLICT_MIN_SHARED:
                return True
    return False


def sanitize_followups(raw) -> list[str]:
    """모델이 낸 후속 질문을 개수·길이 모두 잘라 돌려준다.

    이 값은 DB에 저장된 뒤 버튼으로 그대로 재전송된다. 길이를 안 자르면 질문 상한을 넘는
    제안이 칩으로 그려지고, 누르는 순간 422로 죽는다 — 사용자에겐 그냥 고장으로 보인다.
    """
    return [
        str(f).strip()[:FOLLOWUP_MAX_CHARS]
        for f in (raw or [])
        if str(f).strip()
    ][:MAX_FOLLOWUPS]


def verify(
    model_output: dict, lookup: dict[str, QaChunkRef], *, had_results: bool
) -> VerifiedAnswer:
    """모델 출력 dict → 검증된 답변. 지원 claim만 남기고 최종 상태를 결정한다."""
    # 절단은 마커를 다시 쓴 뒤에 한다(아래). 여기서 자르면 경계에 걸린 '[c1' 조각이
    # 정규식에 매치되지 않아 지워지지도 다시 쓰이지도 못한 채 화면에 남는다.
    answer = str(model_output.get("answer") or "").strip()
    model_status = str(model_output.get("answerStatus") or "").strip()

    verified: list[VerifiedClaim] = []
    # 텍스트 → 이미 채택한 claim_index. 중복 텍스트를 건너뛸 때 그 자리를 가리키던 마커를
    # 살아 있는 동일 claim으로 remap한다 — 그냥 버리면 근거가 있는 문장이 인용을 잃는다.
    seen_text: dict[str, int] = {}
    # 모델이 쓴 마커 번호(모델 자신의 claims 배열 위치) → 최종 claim_index. 비었거나
    # 중복인 항목을 건너뛰는 순간 둘이 어긋나므로, 이 표 없이 마커를 그대로 두면
    # 사용자가 누른 번호가 엉뚱한 근거로 이동한다.
    raw_to_index: dict[int, int] = {}
    idx = 0
    for raw_pos, raw in enumerate((model_output.get("claims") or [])[: MAX_CLAIMS * 2]):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()[:CLAIM_TEXT_MAX_CHARS]
        if not text:
            continue
        if text in seen_text:
            raw_to_index[raw_pos] = seen_text[text]
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
        seen_text[text] = idx
        raw_to_index[raw_pos] = idx
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
    model_flags_conflict = model_status == "conflicting_evidence"
    if not had_results:
        status = "not_found"
    elif not supported:
        status = "insufficient_evidence"
    elif len(supported) >= 2 and (
        model_flags_conflict or claims_conflict([c.text for c in supported])
    ):
        # 모델이 상충을 명시했거나, 양쪽 지원 주장이 같은 대상에 상반된 극성을 보이면
        # 상충으로 확정한다. 모델이 final 힌트를 빠뜨려도 상반 근거가 completed로
        # 노출되지 않게 한다(계약 강화).
        status = "conflicting_evidence"
        for c in supported:
            c.verification_status = QaClaimVerification.CONFLICTING
    else:
        status = "completed"

    followups = sanitize_followups(model_output.get("followUpSuggestions"))

    if status in ("not_found", "insufficient_evidence"):
        # 근거가 없으면 unsupported claim을 확정 사실처럼 저장하지 않는다
        verified = [c for c in verified if c.verification_status != QaClaimVerification.UNSUPPORTED]

    # 마커 정리는 claim 필터링이 끝난 뒤에 돈다 — 걸러진 claim의 마커도 함께 사라져야 한다.
    answer, dropped_citations = _rewrite_citations(answer, verified, raw_to_index)
    answer = _truncate_answer(answer)
    if dropped_citations:
        # 조용히 지우지 않는다. 마커가 통째로 사라지는 회귀를 로그에서 볼 수 있어야 한다.
        # 원문은 남기지 않고 개수만 남긴다.
        logger.info("qa_citations_dropped", dropped=dropped_citations)

    return VerifiedAnswer(
        answer=answer, answer_status=status, claims=verified, followups=followups
    )


def _truncate_answer(answer: str) -> str:
    """상한으로 자르되, 절단 경계에 걸린 마커 조각을 남기지 않는다.

    '…혈압이 상승한다[c1' 같은 꼬리는 정규식에 매치되지 않아 제거 경로를 빠져나가고,
    프론트도 닫는 대괄호가 없으면 본문 글자로 렌더한다 — 사용자가 내부 표기를 그대로 본다.
    """
    if len(answer) <= ANSWER_MAX_CHARS:
        return answer
    cut = answer[:ANSWER_MAX_CHARS]
    return re.sub(r"\[c?\d{0,2}$", "", cut).rstrip()


def _rewrite_citations(
    answer: str, claims: list[VerifiedClaim], raw_to_index: dict[int, int]
) -> tuple[str, int]:
    """마커를 최종 claim_index로 다시 쓰고, 해석되지 않는 마커는 지운다.

    남은 마커는 전부 "검증을 통과해 화면에 보이는 claim"을 가리킨다. 모델이 없는 근거를
    지어내거나 번호를 잘못 써도 화면에는 인용 번호가 뜨지 않는다.
    """
    valid = {
        c.claim_index
        for c in claims
        if c.verification_status != QaClaimVerification.UNSUPPORTED
    }
    dropped = 0

    def _sub(m: re.Match) -> str:
        nonlocal dropped
        target = raw_to_index.get(int(m.group(1)))
        if target is None or target not in valid:
            dropped += 1
            return ""
        return f"[c{target}]"

    return _CITATION_RE.sub(_sub, answer), dropped
