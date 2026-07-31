"""계층형 요약 파이프라인 (순수 로직, DB 없음).

그룹 생성 → map 요약 → reduce 구조화 → 출처 검증 → 수치·대상 결정론적 추출.
저장과 revision 게이트는 서비스 계층이 담당한다. 이 모듈은 chunk 스냅샷만 받는다.
"""

from dataclasses import dataclass

from app.services.summary.grouping import build_groups
from app.services.summary.numbers import (
    extract_number_artifacts,
    extract_population_artifacts,
)
from app.services.summary.provider import (
    ChunkInput,
    DocumentRequest,
    GroupRequest,
    SummaryProvider,
)
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


def run_summary(
    provider: SummaryProvider,
    chunks: list[ChunkSnapshot],
    *,
    learner_level: str,
    language: str,
    include_sections: bool = True,
    include_prerequisites: bool = True,
) -> list[ArtifactDraft]:
    """청크 스냅샷 → 검증된 artifact 초안 목록. 출처 없는 항목은 포함하지 않는다."""
    lookup: dict[str, ChunkRef] = {
        c.chunk_id: ChunkRef(chunk_id=c.chunk_id, source_refs=c.source_refs, text=c.text)
        for c in chunks
    }

    inputs = [
        ChunkInput(
            chunk_id=c.chunk_id,
            section_title=c.section_title,
            text=c.text,
            page_start=c.page_start,
            page_end=c.page_end,
        )
        for c in chunks
    ]
    groups = build_groups(inputs)

    # map: 그룹별 요약. 원본 chunk id를 유지한다.
    group_summaries = [
        provider.summarize_group(
            GroupRequest(
                group_id=g.group_id,
                section_title=g.section_title,
                chunks=g.chunks,
                learner_level=learner_level,
                language=language,
            )
        )
        for g in groups
    ]

    # reduce: 문서 전체 구조화. chunk id 연결이 최종 결과까지 이어진다.
    structured = provider.summarize_document(
        DocumentRequest(
            group_summaries=group_summaries,
            learner_level=learner_level,
            language=language,
            include_sections=include_sections,
            include_prerequisites=include_prerequisites,
        )
    )

    drafts = build_artifacts(structured, lookup, learner_level=learner_level)

    # 수치·대상 집단은 결정론적 추출(원문 검증 포함) — 모델 출력을 쓰지 않는다.
    next_pos = len(drafts)
    number_drafts = extract_number_artifacts(lookup, start_position=next_pos)
    pop_drafts = extract_population_artifacts(
        lookup, start_position=next_pos + len(number_drafts)
    )
    drafts.extend(number_drafts)
    drafts.extend(pop_drafts)

    # 최종 안전장치: 출처 없는 artifact는 절대 남기지 않는다
    return [d for d in drafts if d.source_chunk_ids and d.source_refs]
