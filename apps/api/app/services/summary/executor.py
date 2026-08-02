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
from dataclasses import dataclass, replace

from sqlalchemy import select

from app.core.logging import get_logger
from app.models.document import Document
from app.models.summary import SummaryNode, SummaryRun
from app.services.summary.endpoint import SummaryNetworkError
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
    GROUP_SUMMARY_MAX_CHARS,
    PROMPT_VERSION,
    REDUCE_FAN_IN,
    SCHEMA_VERSION,
)

logger = get_logger(__name__)

# 전체 청크 해시 재계산은 비싸다(청크 수만큼 행 조회). 노드마다 하지 않고 이 간격마다
# 확인하며, 저장 직전에는 summary_job이 반드시 한 번 더 확인한다.
FULL_HASH_CHECK_INTERVAL = 25

# 그룹이 모델 컨텍스트를 넘으면 절반으로 나눠 다시 시도하는 최대 깊이.
#
# Ollama는 가용 메모리에 맞춰 컨텍스트를 **기기마다 다르게** 잡고(같은 0.32.5에서도
# 8192로 뜨는 기기와 더 작게 뜨는 기기가 있다), OpenAI 호환 endpoint로는 요청별 num_ctx를
# 지정할 수도, 실제 값을 조회할 수도 없다. 그래서 상한을 상수로 추측하는 대신 실패했을 때
# 입력을 줄여 적응한다. 깊이 3이면 그룹을 1/8까지 쪼갠다.
MAX_SPLIT_DEPTH = 3


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
            # 레벨 0의 요청 청크는 원본 청크 그 자체다.
            chunk_sources={c.chunk_id: [c.chunk_id] for c in group.chunks},
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
                # 레벨 1+의 요청 청크는 자식 노드다 — 그 노드의 출처가 원본 chunk id다.
                chunk_sources={
                    str(child.node_id): list(child.source_chunk_ids) for child in batch
                },
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
    return finalize_artifacts(
        structured,
        lookup,
        learner_level=learner_level,
        include_sections=include_sections,
        include_prerequisites=include_prerequisites,
    )


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
    chunk_sources: dict[str, list[str]],
    section_title: str | None,
    counter: _Counter,
    guard_args: dict,
) -> _NodeResult:
    """노드 하나: 가드 → 재사용 확인 → (필요 시) 모델 호출 → 체크포인트 저장.

    `chunk_sources`는 요청 청크 id → 서버 소유 원본 chunk id 목록이다. 레벨 0은 자기
    자신, 레벨 1+는 자식 노드의 출처다. 분할·열화로 일부 조각이 버려지면 그 조각의
    출처는 저장하지 않는다.
    """
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

    async def _guard() -> None:
        async with factory() as session:
            await _assert_runnable(session, check_full_hash=False, **guard_args)

    # 모델 호출은 트랜잭션 밖에서 한다 — writer 락을 붙잡은 채 네트워크를 기다리지 않는다.
    try:
        result = await _summarize_adaptively(
            provider, request, document_id=document_id, guard=_guard
        )
    except (SummaryCancelled, SummaryRevisionChanged):
        # 취소·문서 변경은 정상 제어 흐름이다. 여기서 경고로 남기면 취소 한 건마다
        # "미분류 노드 실패" 경고가 쌓여, 실제 모델 실패를 찾을 때 구분되지 않는다.
        raise
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

    text = result.text
    # 출처는 요약문에 실제로 반영된 조각으로 한정한다. 그룹 전체를 그대로 달면 분할·열화
    # 경로에서 요약에 들어가지도 않은 청크가 근거로 남아, 사용자가 '근거 보기'를 눌렀을 때
    # 요약문과 무관한 페이지로 이동하고 그 문장이 거기에 근거한 것으로 오인한다.
    covered_ids = _resolve_covered(result.covered_ids, chunk_sources) or list(source_ids)
    async with factory() as session:
        node = SummaryNode(
            document_id=document_id,
            summary_run_id=run_id,
            level=level,
            position=position,
            # 열화 결과(모델이 만든 완결 요약이 아니라 이어붙인 축약본)는 succeeded로
            # 두지 않는다. 그러면 _find_reusable이 이후 모든 재시도에 같은 축약본을
            # 돌려줘, 사용자가 컨텍스트를 늘려도 더 나은 요약을 받을 수 없다.
            status="degraded" if result.degraded else "succeeded",
            input_hash=input_hash,
            output_hash=_output_hash(text),
            summary_text=text,
            source_chunk_ids_json=list(covered_ids),
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
        source_chunk_ids=list(covered_ids),
        section_title=section_title,
    )


def _resolve_covered(
    covered_request_ids: list[str], chunk_sources: dict[str, list[str]]
) -> list[str]:
    """요약에 반영된 요청 청크 id → 서버 소유 원본 chunk id(순서 유지·중복 제거)."""
    out: list[str] = []
    for cid in covered_request_ids:
        for sid in chunk_sources.get(cid, ()):
            if sid not in out:
                out.append(sid)
    return out


def _is_input_too_large(exc: Exception) -> bool:
    """입력을 줄이면 풀릴 수 있는 실패인지.

    `context_overflow`는 입력이 컨텍스트를 넘었다는 확정 신호다. `map_summary_too_long`은
    모델이 계약보다 길게 썼다는 뜻인데, 입력이 작아지면 답도 짧아지므로 분할이 실제로
    해결책이 된다(분할하지 않으면 temperature 0 그리디 디코딩이라 재시도마다 같은 답이
    나와 영구 실패한다).

    `map_finish_length`도 포함한다. 이것은 공급자가 **더 큰 출력 예산으로 한 번 더 부른
    뒤에도** 잘린 경우만 붙는 reason이다(실기기: 908노드 중 225번째가 3회 연속
    finish_length로 실패해 문서 전체가 매번 버려졌다). 예산을 늘려도 안 되면 남은 수단은
    입력을 줄이는 것뿐이고, 실제로 같은 문서의 다른 그룹은 분할로 통과했다.

    한 번도 재시도하지 않은 날것의 `finish_length`는 **제외한다**. 공급자가 예산을 늘려
    다시 부르는 것이 먼저이고, 그 단계를 건너뛰고 나누면 호출만 증폭된다.
    """
    if not isinstance(exc, SummaryNetworkError):
        return False
    return exc.category == "context_overflow" or exc.reason in (
        "map_summary_too_long",
        "map_finish_length",
    )


@dataclass
class _AdaptiveResult:
    """적응 요약 결과.

    `degraded`면 모델이 만든 완결 요약이 아니라 이어붙인 축약본이다.
    `covered_ids`는 이 요약문에 **실제로 반영된** 입력 청크 id다. 분할·열화 경로에서는
    일부 조각이 버려지므로 요청한 청크 전부가 아니다 — 호출자는 이 값으로 출처를
    계산해야 "요약문은 인용된 청크에서 나온다"는 불변식이 유지된다.
    """

    text: str
    degraded: bool
    covered_ids: list[str]


def _split_point(chunks: list[ChunkInput]) -> int:
    """문자 수가 절반이 되는 지점. 개수로 나누면 큰 청크가 몰린 절반이 안 줄어든다.

    build_groups는 문자 수로 패킹하므로 한 그룹에 5,000자 청크 1개 + 100자 청크 10개가
    들어갈 수 있다. 개수 기준(`len//2`)으로 나누면 앞 절반이 5,400자로 거의 그대로라
    다시 초과하고, 재귀 끝에 그 거대 청크만 남아 통째로 버려진다.
    """
    total = sum(len(c.text) for c in chunks)
    running = 0
    for index, chunk in enumerate(chunks):
        running += len(chunk.text)
        if running * 2 >= total:
            return min(index + 1, len(chunks) - 1)
    return len(chunks) - 1


async def _summarize_adaptively(
    provider,
    request: GroupRequest,
    *,
    document_id: uuid.UUID,
    guard=None,
    depth: int = 0,
) -> _AdaptiveResult:
    """그룹 요약. 입력이 모델 컨텍스트를 넘으면 절반으로 나눠 다시 시도한다.

    Ollama가 기기마다 다른 컨텍스트로 모델을 올리고 서버는 그 값을 알 수 없으므로,
    상한을 추측하는 대신 실패했을 때 적응한다. 나눈 요약들은 다시 한 번 합쳐 노드
    하나당 요약 하나라는 계약을 유지한다(출처는 호출자가 서버 소유로 계산하되,
    여기서 돌려주는 covered_ids 범위로 한정한다).
    """
    if guard is not None:
        # 재귀 한 번에 최대 22회 호출이 나갈 수 있다. 가드가 노드 진입 시 1회뿐이면
        # 취소·문서 변경 후에도 수십 분간 모델을 계속 돌린다.
        await guard()
    all_ids = [c.chunk_id for c in request.chunks]
    try:
        summary = await asyncio.to_thread(provider.summarize_group, request)
        return _AdaptiveResult(
            text=summary.summary_text, degraded=False, covered_ids=all_ids
        )
    except Exception as exc:
        if (
            not _is_input_too_large(exc)
            or depth >= MAX_SPLIT_DEPTH
            or len(request.chunks) < 2
        ):
            raise
        logger.info(
            "summary_group_split_retry",
            document_id=str(document_id),
            group_id=request.group_id,
            depth=depth,
            input_chunks=len(request.chunks),
            input_chars=sum(len(c.text) for c in request.chunks),
            failure_reason=getattr(exc, "reason", None) or "none",
        )

    mid = _split_point(request.chunks)
    parts: list[_AdaptiveResult] = []
    degraded = False
    last_failure: Exception | None = None
    for index, half in enumerate((request.chunks[:mid], request.chunks[mid:])):
        # 자식 실패를 여기서 잡지 않으면 아래 병합 폴백에 도달하지 못하고, 이미 비용을
        # 지불한 형제 부분 요약까지 함께 버려진다.
        try:
            child = await _summarize_adaptively(
                provider,
                replace(request, group_id=f"{request.group_id}s{index}", chunks=half),
                document_id=document_id,
                guard=guard,
                depth=depth + 1,
            )
        except SummaryCancelled:
            raise  # 취소는 그대로 올린다 — 부분 결과로 이어가면 안 된다
        except SummaryRevisionChanged:
            raise
        except Exception as exc:
            if not _is_input_too_large(exc):
                raise
            logger.info(
                "summary_group_part_dropped",
                document_id=str(document_id),
                group_id=f"{request.group_id}s{index}",
                depth=depth + 1,
                failure_reason=getattr(exc, "reason", None) or "none",
            )
            degraded = True
            last_failure = exc
            continue
        parts.append(child)
        degraded = degraded or child.degraded

    if not parts:
        # 어느 조각도 요약하지 못했다 — 지어내지 않고 실패로 올린다. 원래 예외를 그대로
        # 올린다: 범주를 context_overflow로 덮으면 출력 길이 계약 위반 같은 메모리와
        # 무관한 실패에 "메모리를 확보하라"는 실행 불가 안내가 뜨고, 로그의 실제
        # failure_reason도 사라진다.
        raise last_failure or SummaryNetworkError("context_overflow", "map_context_overflow")

    # 나눈 요약을 다시 하나로 — 입력이 훨씬 짧아 같은 계약으로 통과한다.
    covered = _merge_ids(parts)
    merged = replace(
        request,
        chunks=[
            ChunkInput(
                chunk_id=f"{request.group_id}p{i}",
                section_title=request.section_title,
                text=part.text,
                page_start=0,
                page_end=0,
            )
            for i, part in enumerate(parts)
        ],
    )
    try:
        summary = await asyncio.to_thread(provider.summarize_group, merged)
        return _AdaptiveResult(
            text=summary.summary_text, degraded=degraded, covered_ids=covered
        )
    except Exception as exc:
        if not _is_input_too_large(exc):
            raise
        # 병합마저 거부되면 더 부를수록 손해다. 이미 만든 부분 요약을 이어 붙인다 —
        # 문서 전체 요약을 잃는 것보다 낫다. 단, 계약 길이에 **들어가는 조각만** 담고
        # 출처도 그 조각으로 한정한다. 이어붙인 뒤 잘라내면 잘려나간 조각의 청크가
        # 요약에 없는데도 근거로 남아, 사용자가 '근거 보기'에서 요약문과 무관한
        # 페이지로 이동하게 된다.
        kept = _pack_within_contract(parts)
        logger.info(
            "summary_group_merge_fallback",
            document_id=str(document_id),
            group_id=request.group_id,
            parts=len(parts),
            kept_parts=len(kept),
            failure_reason=getattr(exc, "reason", None) or "none",
        )
        if not kept:
            # 첫 조각조차 상한을 넘는다(모델이 계약을 어긴 경우) — 그 조각만 경계에서
            # 자르고 출처도 그 조각으로 한정한다.
            first = parts[0]
            return _AdaptiveResult(
                text=_truncate_on_boundary(first.text.strip()),
                degraded=True,
                covered_ids=list(first.covered_ids),
            )
        return _AdaptiveResult(
            text=" ".join(p.text.strip() for p in kept),
            degraded=True,
            covered_ids=_merge_ids(kept),
        )


def _merge_ids(parts: list[_AdaptiveResult]) -> list[str]:
    """부분 결과들의 covered_ids 합집합(순서 유지)."""
    out: list[str] = []
    for part in parts:
        for cid in part.covered_ids:
            if cid not in out:
                out.append(cid)
    return out


def _pack_within_contract(parts: list[_AdaptiveResult]) -> list[_AdaptiveResult]:
    """계약 길이 안에 온전히 들어가는 앞쪽 조각들만 남긴다(중간 절단 없음)."""
    kept: list[_AdaptiveResult] = []
    used = 0
    for part in parts:
        text = part.text.strip()
        if not text:
            continue
        extra = len(text) + (1 if kept else 0)  # 조각 사이 공백 1자
        if used + extra > GROUP_SUMMARY_MAX_CHARS:
            break
        kept.append(part)
        used += extra
    return kept


def _truncate_on_boundary(text: str) -> str:
    """계약 길이로 자르되 마지막 공백까지만 남긴다(문장 중간 절단 완화)."""
    if len(text) <= GROUP_SUMMARY_MAX_CHARS:
        return text
    cut = text[:GROUP_SUMMARY_MAX_CHARS]
    space = cut.rfind(" ")
    return (cut[:space] if space > GROUP_SUMMARY_MAX_CHARS // 2 else cut).rstrip()


async def _bump_completed(session, run_id: uuid.UUID) -> None:
    run = await session.get(SummaryRun, run_id)
    if run is not None:
        run.completed_nodes = (run.completed_nodes or 0) + 1
