"""구조화 출력 파싱·검증 + 출처 재구성.

핵심 안전 규칙:
- 모델 출력에서 chunk id만 받는다. page/bbox는 절대 신뢰하지 않고, 검증된 chunk의
  저장된 source_refs를 서버가 재조회해 출처를 만든다.
- 알 수 없는/타 문서 chunk id 거부, 빈 출처·빈 내용 항목 제거, 최대 길이 제한, 중복 제거.
- 전체 coverage와 별개로 artifact별 대표 chunk/ref를 결정론적 상한으로 제한한다.
"""

import re
from dataclasses import dataclass, field
from typing import TypeVar

from app.core.logging import get_logger
from app.models.enums import SummaryArtifactType
from app.services.summary.settings import (
    CONCEPT_EXPLANATION_MAX_CHARS,
    GENERIC_TEXT_MAX_CHARS,
    OVERVIEW_MAX_CHARS,
    SECTION_SUMMARY_MAX_CHARS,
    SUMMARY_EVIDENCE_CHUNK_LIMIT,
    SUMMARY_EVIDENCE_REF_LIMIT,
)

logger = get_logger(__name__)
T = TypeVar("T")


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

    def __post_init__(self) -> None:
        """모든 저장 경로에 상한을 적용한다(새 artifact 생산자의 누락도 fail-safe)."""
        self.source_chunk_ids = bounded_evidence_chunk_ids(self.source_chunk_ids)
        self.source_refs = bounded_source_refs(self.source_refs)


def _spread_sample(items: list[T], limit: int) -> list[T]:
    """처음·끝을 포함해 입력 전 범위에서 결정론적으로 ``limit``개를 고른다."""
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return list(items)
    if limit == 1:
        return [items[0]]
    last = len(items) - 1
    return [items[index * last // (limit - 1)] for index in range(limit)]


def bounded_evidence_chunk_ids(
    raw_ids: list[str], *, limit: int = SUMMARY_EVIDENCE_CHUNK_LIMIT
) -> list[str]:
    """서버가 계산한 coverage에서 문서 전반을 대표하는 작은 id 집합을 만든다."""
    seen: set[str] = set()
    out: list[str] = []
    for sid in raw_ids:
        if isinstance(sid, str) and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return _spread_sample(out, limit)


def _valid_ids(raw_ids, lookup: dict[str, ChunkRef]) -> list[str]:
    """실제 출처가 있는 현재 문서 id만 검증한 뒤 대표 근거 상한을 적용한다."""
    if not isinstance(raw_ids, list):
        return []
    valid = [
        sid
        for sid in raw_ids
        if isinstance(sid, str) and sid in lookup and lookup[sid].source_refs
    ]
    return bounded_evidence_chunk_ids(valid)


def _ref_key(ref: dict) -> tuple:
    bbox = ref.get("bbox")
    return (
        ref.get("pageNumber"),
        ref.get("blockId"),
        tuple(bbox) if isinstance(bbox, list | tuple) else (),
    )


def bounded_source_refs(
    raw_refs: list[dict], *, limit: int = SUMMARY_EVIDENCE_REF_LIMIT
) -> list[dict]:
    """저장된/레거시 source ref를 중복 제거하고 API 안전 상한으로 제한한다."""
    unique: list[dict] = []
    seen: set[tuple] = set()
    for ref in raw_refs:
        if not isinstance(ref, dict):
            continue
        key = _ref_key(ref)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return _spread_sample(unique, limit)


def _refs_for(ids: list[str], lookup: dict[str, ChunkRef]) -> list[dict]:
    """각 대표 청크의 첫 위치와 추가 위치를 작은 상한 안에서 재구성한다."""
    primary: list[dict] = []
    extras: list[dict] = []
    seen: set[tuple] = set()
    for cid in ids:
        first_for_chunk = True
        for ref in lookup[cid].source_refs:
            if not isinstance(ref, dict):
                continue
            key = _ref_key(ref)
            if key not in seen:
                seen.add(key)
                if first_for_chunk:
                    primary.append(ref)
                    first_for_chunk = False
                else:
                    extras.append(ref)

    if len(primary) >= SUMMARY_EVIDENCE_REF_LIMIT:
        return _spread_sample(primary, SUMMARY_EVIDENCE_REF_LIMIT)
    remaining = SUMMARY_EVIDENCE_REF_LIMIT - len(primary)
    return [*primary, *_spread_sample(extras, remaining)]


# 문장 끝. 한국어 종결(다./요.)과 서양식 문장부호를 함께 본다.
_SENTENCE_END = re.compile(r"(?:[.!?。]|다\.|요\.)\s")


def _clean_text(value, limit: int, *, field_name: str = "?") -> str:
    """모델의 arbitrary value를 문자열로 강제 변환하거나 문장 중간에서 자르지 않는다.

    상한을 넘기면 **문장 경계까지만** 남긴다. 아무 데서나 자르면 의료 문장에서 의미가
    뒤집힌다("신부전 환자에게 투여 금기다" → "신부전 환자에게 투여 금"). 반대로 항목을
    통째로 버리면 남길 수 있었던 문장까지 사라지고, 사용자는 개요가 없는 요약을 오류
    없이 받는다 — 노드가 열화된 게 아니라서 부분 완료 안내조차 뜨지 않는다.

    자를 문장 경계가 없으면(한 문장이 통째로 상한을 넘는 경우) 그때는 버린다.
    조용히 버리지 않도록 흔적을 남긴다 — 원문은 남기지 않고 길이만 기록한다.
    """
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return cleaned

    boundaries = [m.end() for m in _SENTENCE_END.finditer(cleaned[: limit + 1])]
    if boundaries:
        logger.info(
            "summary_artifact_truncated_too_long",
            field=field_name,
            length=len(cleaned),
            limit=limit,
            kept=boundaries[-1],
        )
        return cleaned[: boundaries[-1]].strip()

    logger.info(
        "summary_artifact_dropped_too_long",
        field=field_name,
        length=len(cleaned),
        limit=limit,
    )
    return ""


def build_artifacts(
    structured: dict,
    lookup: dict[str, ChunkRef],
    *,
    learner_level: str,
    include_sections: bool = True,
    include_prerequisites: bool = True,
) -> list[ArtifactDraft]:
    """구조화 dict → 검증된 artifact 초안 목록. 출처 없는/빈 항목은 제외한다.

    `include_*`는 사용자가 요청에서 끈 항목이다. 스키마·프롬프트로 막더라도 모델이
    보내오면 저장돼 화면에 뜨므로, 저장 직전에도 게이트를 둔다(스키마가 강제되지 않는
    외부·비-Ollama 경로에서는 프롬프트가 유일한 방어라 실제로 새어 들어왔다).
    """
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
        text = _clean_text(overview.get("text"), OVERVIEW_MAX_CHARS, field_name="overview.text")
        if text:
            add(
                SummaryArtifactType.OVERVIEW,
                None,
                {"text": text},
                overview.get("sourceChunkIds"),
            )

    # sections
    seen_sections: set[tuple[str, str]] = set()
    for sec in (structured.get("sections") or []) if include_sections else []:
        if not isinstance(sec, dict):
            continue
        title = _clean_text(sec.get("title"), 300) or None
        summary = _clean_text(
            sec.get("summary"), SECTION_SUMMARY_MAX_CHARS, field_name="section.summary"
        )
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
        explanation = _clean_text(
            kc.get("explanation"),
            CONCEPT_EXPLANATION_MAX_CHARS,
            field_name="keyConcept.explanation",
        )
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
    for pr in (structured.get("prerequisites") or []) if include_prerequisites else []:
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
