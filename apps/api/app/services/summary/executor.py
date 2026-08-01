"""저장형 계층 요약 실행기 — 노드 단위 체크포인트로 대형 문서를 처리한다.

설계 원칙

- **짧은 트랜잭션**: 노드 하나를 처리할 때마다 세션을 열고 닫는다. 장시간 writer
  트랜잭션은 같은 SQLite 파일의 설정 저장과 경합해 실제로 활성화 실패를 만들었다
  (`docs/testing/windows-local-ai-activation-diagnosis.md` §2).
- **재사용**: 노드 키(input_hash)가 같은 성공 노드가 이미 있으면 모델을 부르지 않고
  결과를 복사한다. 앱을 껐다 켜거나 실패 후 재시도해도 처음부터 다시 돌지 않는다.
- **서버 소유 출처**: 레벨 0은 그룹에 든 chunk id, 레벨 1+는 자식 출처의 합집합을
  서버가 계산한다. 모델이 돌려준 id는 쓰지 않는다.
- **취소·revision 확인**: 모델 호출 사이마다 잡이 여전히 유효한지 확인하고, 문서가
  바뀌면 중간 결과를 저장하지 않고 중단한다.
"""

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.core.logging import get_logger
from app.models.document import Document
from app.models.summary import SummaryNode, SummaryRun
from app.services.summary.grouping import build_groups
from app.services.summary.hierarchy import (
    build_context_key,
    fan_in_batches,
    map_node_input_hash,
    plan_total_nodes,
    reduce_node_input_hash,
)
from app.services.summary.pipeline import (
    ChunkSnapshot,
    build_chunk_inputs,
    build_chunk_lookup,
    finalize_artifacts,
)
from app.services.summary.provider import (
    ChunkInput,
    DocumentRequest,
    GroupRequest,
    GroupSummary,
)
from app.services.summary.schema import ArtifactDraft
from app.services.summary.settings import (
    PROMPT_VERSION,
    REDUCE_FAN_IN,
    SCHEMA_VERSION,
)

logger = get_logger(__name__)

# 전체 청크 해시 재계산은 비싸다(청크 수만큼 행 조회). 노드마다 하지 않고 이 간격마다
# 확인하며, 저장 직전에는 summary_job이 반드시 한 번 더 확인한다.
FULL_HASH_CHECK_INTERVAL = 25


class SummaryCancelled(Exception):
    """잡이 취소·교체됨 — 실패로 기록하지 않고 조용히 종료한다."""


class SummaryRevisionChanged(Exception):
    """실행 중 문서가 바뀜 — 중간 결과를 저장하지 않는다."""


class SummaryNoContent(Exception):
    """요약할 본문이 없다."""


@dataclass
class _NodeResult:
    """실행된(또는 재사용된) 노드 한 개."""

    node_id: uuid.UUID
    input_hash: str
    summary_text: str
    source_chunk_ids: list[str]
    section_title: str | None


async def _assert_runnable(
    session,
    *,
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    start_revision: int,
    start_hash: str,
    check_full_hash: bool,
    job_is_current,
    current_chunk_hash,
) -> None:
    """모델 호출 직전 가드 — 취소·교체·revision 변경을 감지한다."""
    if not await job_is_current(session, document_id, job_id):
        raise SummaryCancelled
    doc = await session.get(Document, document_id)
    if doc is None or doc.deleted_at is not None:
        raise SummaryCancelled
    if doc.content_revision != start_revision:
        raise SummaryRevisionChanged
    if check_full_hash and await current_chunk_hash(session, document_id) != start_hash:
        raise SummaryRevisionChanged


async def _find_reusable(session, document_id: uuid.UUID, input_hash: str) -> SummaryNode | None:
    """같은 문서에서 같은 입력으로 이미 성공한 노드를 찾는다."""
    return (
        await session.execute(
            select(SummaryNode)
            .where(
                SummaryNode.document_id == document_id,
                SummaryNode.input_hash == input_hash,
                SummaryNode.status == "succeeded",
            )
            .limit(1)
        )
    ).scalars().first()


def _output_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def execute_hierarchical_summary(
    factory,
    *,
    document_id: uuid.UUID,
    run_id: uuid.UUID,
    job_id: uuid.UUID,
    provider,
    chunks: list[ChunkSnapshot],
    learner_level: str,
    language: str,
    include_sections: bool,
    include_prerequisites: bool,
    start_revision: int,
    start_hash: str,
    job_is_current,
    current_chunk_hash,
) -> list[ArtifactDraft]:
    """체크포인트 계층 요약 실행 → 검증된 artifact 초안 목록.

    호출자(summary_job)는 반환값을 revision 가드 아래에서 원자적으로 저장한다.
    """
    lookup = build_chunk_lookup(chunks)
    groups = build_groups(build_chunk_inputs(chunks))
    if not groups:
        raise SummaryNoContent

    context_key = build_context_key(
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        learner_level=learner_level,
        language=language,
        source_revision=start_revision,
        source_chunk_hash=start_hash,
    )

    planned = plan_total_nodes(len(groups), REDUCE_FAN_IN)
    async with factory() as session:
        run = await session.get(SummaryRun, run_id)
        if run is not None:
            run.planned_nodes = planned
            run.completed_nodes = 0
            await session.commit()

    group_chars = [g.char_count for g in groups]
    logger.info(
        "summary_hierarchy_planned",
        document_id=str(document_id),
        chunks=len(chunks),
        groups=len(groups),
        planned_nodes=planned,
        # 그룹 크기 분포 — 모델 컨텍스트 초과 진단에 필요하다(가장 큰 그룹이 상한이다).
        group_chars_max=max(group_chars),
        group_chars_avg=sum(group_chars) // len(group_chars),
    )

    guard_args = dict(
        document_id=document_id,
        job_id=job_id,
        start_revision=start_revision,
        start_hash=start_hash,
        job_is_current=job_is_current,
        current_chunk_hash=current_chunk_hash,
    )
    counter = _Counter()

    # --- 레벨 0: 청크 그룹 map ---
    level_nodes: list[_NodeResult] = []
    for position, group in enumerate(groups):
        input_hash = map_node_input_hash(
            context_key, [(c.chunk_id, c.text) for c in group.chunks]
        )
        source_ids = list(dict.fromkeys(c.chunk_id for c in group.chunks))
        request = GroupRequest(
            group_id=group.group_id,
            section_title=group.section_title,
            chunks=group.chunks,
            learner_level=learner_level,
            language=language,
        )
        node = await _process_node(
            factory,
            provider=provider,
            run_id=run_id,
            document_id=document_id,
            level=0,
            position=position,
            input_hash=input_hash,
            request=request,
            source_ids=source_ids,
            section_title=group.section_title,
            counter=counter,
            guard_args=guard_args,
        )
        level_nodes.append(node)

    # --- 레벨 1+: bounded fan-in reduce ---
    level = 1
    while len(level_nodes) > REDUCE_FAN_IN:
        next_nodes: list[_NodeResult] = []
        for position, batch in enumerate(fan_in_batches(level_nodes, REDUCE_FAN_IN)):
            input_hash = reduce_node_input_hash(
                context_key, level, [(n.input_hash, n.summary_text) for n in batch]
            )
            # 출처는 자식 출처의 합집합 — 서버가 계산하고 모델 출력은 쓰지 않는다.
            merged_source_ids: list[str] = []
            for child in batch:
                for cid in child.source_chunk_ids:
                    if cid not in merged_source_ids:
                        merged_source_ids.append(cid)
            request = GroupRequest(
                group_id=f"L{level}n{position}",
                section_title=batch[0].section_title,
                chunks=[
                    ChunkInput(
                        chunk_id=str(child.node_id),
                        section_title=child.section_title,
                        text=child.summary_text,
                        page_start=0,
                        page_end=0,
                    )
                    for child in batch
                ],
                learner_level=learner_level,
                language=language,
            )
            node = await _process_node(
                factory,
                provider=provider,
                run_id=run_id,
                document_id=document_id,
                level=level,
                position=position,
                input_hash=input_hash,
                request=request,
                source_ids=merged_source_ids,
                section_title=batch[0].section_title,
                counter=counter,
                guard_args=guard_args,
            )
            next_nodes.append(node)
        level_nodes = next_nodes
        level += 1

    # --- 구조화 reduce: 마지막 레벨(≤ fan-in)을 한 번에 문서 요약으로 ---
    async with factory() as session:
        await _assert_runnable(session, check_full_hash=True, **guard_args)

    group_summaries = [
        GroupSummary(
            group_id=f"g{i}",
            section_title=n.section_title,
            summary_text=n.summary_text,
            source_chunk_ids=n.source_chunk_ids,
        )
        for i, n in enumerate(level_nodes)
    ]
    structured = await asyncio.to_thread(
        provider.summarize_document,
        DocumentRequest(
            group_summaries=group_summaries,
            learner_level=learner_level,
            language=language,
            include_sections=include_sections,
            include_prerequisites=include_prerequisites,
        ),
    )
    return finalize_artifacts(structured, lookup, learner_level=learner_level)


class _Counter:
    """호출 사이 전체 해시 확인 주기를 세는 카운터."""

    def __init__(self) -> None:
        self.processed = 0

    def next_needs_full_check(self) -> bool:
        return self.processed % FULL_HASH_CHECK_INTERVAL == 0


async def _process_node(
    factory,
    *,
    provider,
    run_id: uuid.UUID,
    document_id: uuid.UUID,
    level: int,
    position: int,
    input_hash: str,
    request: GroupRequest,
    source_ids: list[str],
    section_title: str | None,
    counter: _Counter,
    guard_args: dict,
) -> _NodeResult:
    """노드 하나: 가드 → 재사용 확인 → (필요 시) 모델 호출 → 체크포인트 저장."""
    async with factory() as session:
        await _assert_runnable(
            session, check_full_hash=counter.next_needs_full_check(), **guard_args
        )
        reusable = await _find_reusable(session, document_id, input_hash)
        if reusable is not None:
            node = SummaryNode(
                document_id=document_id,
                summary_run_id=run_id,
                level=level,
                position=position,
                status="succeeded",
                input_hash=input_hash,
                output_hash=reusable.output_hash,
                summary_text=reusable.summary_text,
                # 출처는 재사용본이 아니라 이번 실행에서 계산한 값을 쓴다(서버 소유).
                source_chunk_ids_json=list(source_ids),
                attempt_count=0,
                reused=True,
            )
            session.add(node)
            await _bump_completed(session, run_id)
            await session.commit()
            await session.refresh(node)
            counter.processed += 1
            return _NodeResult(
                node_id=node.id,
                input_hash=input_hash,
                summary_text=reusable.summary_text,
                source_chunk_ids=list(source_ids),
                section_title=section_title,
            )

    # 모델 호출은 트랜잭션 밖에서 한다 — writer 락을 붙잡은 채 네트워크를 기다리지 않는다.
    try:
        summary = await asyncio.to_thread(provider.summarize_group, request)
    except Exception as exc:
        # 어느 노드에서 죽었는지 남긴다. 이게 없으면 "요약 실패"만 보이고 첫 호출에서
        # 실패했는지 수백 번째에서 실패했는지 구분할 수 없다(원인 범위가 완전히 다르다).
        logger.warning(
            "summary_node_failed",
            document_id=str(document_id),
            level=level,
            position=position,
            completed_before=counter.processed,
            input_chunks=len(request.chunks),
            input_chars=sum(len(c.text) for c in request.chunks),
            error_type=type(exc).__name__,
            failure_category=getattr(exc, "category", None) or "unexpected",
            failure_reason=getattr(exc, "reason", None) or "none",
        )
        raise
    text = summary.summary_text

    async with factory() as session:
        node = SummaryNode(
            document_id=document_id,
            summary_run_id=run_id,
            level=level,
            position=position,
            status="succeeded",
            input_hash=input_hash,
            output_hash=_output_hash(text),
            summary_text=text,
            source_chunk_ids_json=list(source_ids),
            attempt_count=1,
            reused=False,
        )
        session.add(node)
        await _bump_completed(session, run_id)
        await session.commit()
        await session.refresh(node)
    counter.processed += 1
    return _NodeResult(
        node_id=node.id,
        input_hash=input_hash,
        summary_text=text,
        source_chunk_ids=list(source_ids),
        section_title=section_title,
    )


async def _bump_completed(session, run_id: uuid.UUID) -> None:
    run = await session.get(SummaryRun, run_id)
    if run is not None:
        run.completed_nodes = (run.completed_nodes or 0) + 1
