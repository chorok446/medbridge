"""구조화 출력 파싱·검증 + 출처 재구성.

핵심 안전 규칙:
- 모델 출력에서 chunk id만 받는다. page/bbox는 절대 신뢰하지 않고, 검증된 chunk의
  저장된 source_refs를 서버가 재조회해 출처를 만든다.
- 알 수 없는/타 문서 chunk id 거부, 빈 출처·빈 내용 항목 제거, 최대 길이 제한, 중복 제거.
"""

from dataclasses import dataclass, field

from app.models.enums import SummaryArtifactType
from app.services.summary.settings import (
    CONCEPT_EXPLANATION_MAX_CHARS,
    GENERIC_TEXT_MAX_CHARS,
    OVERVIEW_MAX_CHARS,
    SECTION_SUMMARY_MAX_CHARS,
)


@dataclass
class ChunkRef:
    """검증용 청크 정보 — 문서 소속 여부 판정과 source_refs 재구성에 쓴다."""

    chunk_id: str
    source_refs: list[dict]
    text: str


@dataclass
class ArtifactDraft:
    artifact_type: SummaryArtifactType
    title: str | None
    position: int
    content_json: dict
    source_chunk_ids: list[str]
    source_refs: list[dict] = field(default_factory=list)


def _valid_ids(raw_ids, lookup: dict[str, ChunkRef]) -> list[str]:
    """문서에 실제 존재하는 chunk id만, 순서를 유지하며 중복 없이 남긴다."""
    if not isinstance(raw_ids, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for sid in raw_ids:
        if not isinstance(sid, str):
            continue
        if sid in lookup and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _refs_for(ids: list[str], lookup: dict[str, ChunkRef]) -> list[dict]:
    """검증된 chunk id들의 저장된 source_refs를 합친다(중복 제거). 모델 출력이 아니다."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for cid in ids:
        for ref in lookup[cid].source_refs:
            key = (
                ref.get("pageNumber"),
                ref.get("blockId"),
                tuple(ref.get("bbox") or []),
            )
            if key not in seen:
                seen.add(key)
                out.append(ref)
    return out


def _clean_text(value, limit: int) -> str:
    # 모델의 arbitrary value를 문자열로 강제 변환하거나 조용히 자르지 않는다.
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit:
        return ""
    return cleaned


def build_artifacts(
    structured: dict, lookup: dict[str, ChunkRef], *, learner_level: str
) -> list[ArtifactDraft]:
    """구조화 dict → 검증된 artifact 초안 목록. 출처 없는/빈 항목은 제외한다."""
    drafts: list[ArtifactDraft] = []
    position = 0

    def add(atype, title, content, ids) -> None:
        nonlocal position
        valid = _valid_ids(ids, lookup)
        if not valid:
            return  # 출처 없는 artifact는 저장하지 않는다
        drafts.append(
            ArtifactDraft(
                artifact_type=atype,
                title=title,
                position=position,
                content_json=content,
                source_chunk_ids=valid,
                source_refs=_refs_for(valid, lookup),
            )
        )
        position += 1

    # overview (단일)
    overview = structured.get("overview")
    if isinstance(overview, dict):
        text = _clean_text(overview.get("text"), OVERVIEW_MAX_CHARS)
        if text:
            add(
                SummaryArtifactType.OVERVIEW,
                None,
                {"text": text},
                overview.get("sourceChunkIds"),
            )

    # sections
    seen_sections: set[tuple[str, str]] = set()
    for sec in structured.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        title = _clean_text(sec.get("title"), 300) or None
        summary = _clean_text(sec.get("summary"), SECTION_SUMMARY_MAX_CHARS)
        if not summary:
            continue
        dedup_key = (title or "", summary)
        if dedup_key in seen_sections:
            continue
        seen_sections.add(dedup_key)
        add(
            SummaryArtifactType.SECTION_SUMMARY,
            title,
            {"summary": summary},
            sec.get("sourceChunkIds"),
        )

    # key concepts
    seen_terms: set[str] = set()
    for kc in structured.get("keyConcepts") or []:
        if not isinstance(kc, dict):
            continue
        term = _clean_text(kc.get("term"), 200)
        explanation = _clean_text(kc.get("explanation"), CONCEPT_EXPLANATION_MAX_CHARS)
        if not term or not explanation or term in seen_terms:
            continue
        seen_terms.add(term)
        add(
            SummaryArtifactType.KEY_CONCEPT,
            term,
            {"term": term, "explanation": explanation},
            kc.get("sourceChunkIds"),
        )

    # prerequisites — 기본 저장 대상은 document 근거 항목만
    for pr in structured.get("prerequisites") or []:
        if not isinstance(pr, dict):
            continue
        concept = _clean_text(pr.get("concept"), 200)
        why = _clean_text(pr.get("whyNeeded"), GENERIC_TEXT_MAX_CHARS)
        source_type = pr.get("sourceType") or "document"
        if not concept or source_type != "document":
            continue
        add(
            SummaryArtifactType.PREREQUISITE,
            concept,
            {"concept": concept, "whyNeeded": why, "sourceType": "document"},
            pr.get("sourceChunkIds"),
        )

    # 중요 수치·대상 집단(importantNumbers/targetPopulations)은 모델 출력을 쓰지 않는다 —
    # 파이프라인의 결정론적 추출(numbers.py)이 원문 검증과 함께 담당한다(§7).

    # learner explanations
    for le in structured.get("learnerExplanations") or []:
        if not isinstance(le, dict):
            continue
        text = _clean_text(le.get("text"), GENERIC_TEXT_MAX_CHARS)
        if not text:
            continue
        level = le.get("level") or learner_level
        if not isinstance(level, str):
            continue
        add(
            SummaryArtifactType.LEARNER_EXPLANATION,
            level,
            {"level": level, "text": text},
            le.get("sourceChunkIds"),
        )

    # 모델의 studyCautions는 저장하지 않는다. 안전 고지는 문서 근거처럼 꾸며진 artifact가
    # 아니라 SummaryView의 시스템 소유 고정 배너로 항상 표시한다.

    return drafts
