"""계층형 요약 파이프라인의 공용 순수 로직 (DB 없음).

실행은 `app.services.tasks.summary_job`의 체크포인트 실행기가 map/reduce를 단계별로
저장하며 담당하고, 이 모듈은 그 경로가 쓰는 `build_chunk_lookup`·`build_chunk_inputs`·
`finalize_artifacts`(출처 검증·수치 추출 규칙)만 제공한다. 저장과 revision 게이트는
서비스 계층이 담당한다.
"""

from dataclasses import dataclass

from app.services.summary.numbers import (
    extract_number_artifacts,
    extract_population_artifacts,
)
from app.services.summary.provider import ChunkInput
from app.services.summary.schema import ArtifactDraft, ChunkRef, build_artifacts


@dataclass
class ChunkSnapshot:
    """요약 입력 청크 — 서비스가 document_chunks에서 만든 스냅샷."""

    chunk_id: str
    section_title: str | None
    text: str
    page_start: int
    page_end: int
    source_refs: list[dict]


def build_chunk_lookup(chunks: list[ChunkSnapshot]) -> dict[str, ChunkRef]:
    """chunk_id → 서버 소유 출처. 모델 출력은 이 표에 있는 id만 통과한다."""
    return {
        c.chunk_id: ChunkRef(chunk_id=c.chunk_id, source_refs=c.source_refs, text=c.text)
        for c in chunks
    }


def build_chunk_inputs(chunks: list[ChunkSnapshot]) -> list[ChunkInput]:
    return [
        ChunkInput(
            chunk_id=c.chunk_id,
            section_title=c.section_title,
            text=c.text,
            page_start=c.page_start,
            page_end=c.page_end,
        )
        for c in chunks
    ]


def finalize_artifacts(
    structured: dict,
    lookup: dict[str, ChunkRef],
    *,
    learner_level: str,
    include_sections: bool = True,
    include_prerequisites: bool = True,
) -> list[ArtifactDraft]:
    """구조화 요약 → 검증된 artifact 초안. 출처 없는 항목은 절대 남기지 않는다."""
    drafts = build_artifacts(
        structured,
        lookup,
        learner_level=learner_level,
        include_sections=include_sections,
        include_prerequisites=include_prerequisites,
    )

    # 수치·대상 집단은 결정론적 추출(원문 검증 포함) — 모델 출력을 쓰지 않는다.
    next_pos = len(drafts)
    number_drafts = extract_number_artifacts(lookup, start_position=next_pos)
    pop_drafts = extract_population_artifacts(lookup, start_position=next_pos + len(number_drafts))
    drafts.extend(number_drafts)
    drafts.extend(pop_drafts)

    # 최종 안전장치: 출처 없는 artifact는 절대 남기지 않는다
    return [d for d in drafts if d.source_chunk_ids and d.source_refs]
