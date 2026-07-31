"""모델 출력 파싱·서버측 출처 검증.

모델 결과를 절대 그대로 저장하지 않는다. 각 claim의 sourceChunkIds가 이번 검색 청크의
부분집합이며 현재 문서 소속인지 확인하고, page/bbox는 저장된 source_refs에서 재구성한다.
수치 주장은 출처 청크 원문에 실제로 존재하는지 검증한다.
"""

from dataclasses import dataclass, field

from app.models.enums import QaClaimVerification
from app.services.qa.context import QaChunkRef
from app.services.qa.settings import (
    ANSWER_MAX_CHARS,
    CLAIM_TEXT_MAX_CHARS,
    MAX_CLAIMS,
    MAX_FOLLOWUPS,
)
from app.services.summary.numbers import _NUMBER_RE


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
    """claim의 수치가 근거 청크 원문에 실제로 존재하는지. 수치가 없으면 통과."""
    numbers = {m.group(0).strip() for m in _NUMBER_RE.finditer(claim_text)}
    if not numbers:
        return True
    haystack = "\n".join(lookup[c].text for c in ids)
    return all(n in haystack for n in numbers)


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
        # 출처 없음 → unsupported / 수치가 원문에 없음 → unsupported
        if not ids or not _numbers_present(text, ids, lookup):
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
