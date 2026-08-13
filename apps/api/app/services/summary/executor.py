"""저장형 계층 요약 실행기 — 노드 단위 체크포인트로 대형 문서를 처리한다.

설계 원칙

- **짧은 트랜잭션**: 노드 하나를 처리할 때마다 세션을 열고 닫는다. 장시간 writer
  트랜잭션은 같은 SQLite 파일의 설정 저장과 경합해 실제로 활성화 실패를 만들었다
  (`docs/testing/windows-local-ai-activation-diagnosis.md` §2).
- **재사용**: 노드 키(input_hash)가 같은 성공 노드가 이미 있으면 모델을 부르지 않고
  결과를 복사한다. 앱을 껐다 켜거나 실패 후 재시도해도 처음부터 다시 돌지 않는다.
- **coverage/evidence 분리**: 레벨 0은 그룹에 든 chunk id, 레벨 1+는 자식 coverage의
  합집합을 메모리에서 온전히 유지한다. DB와 사용자 산출물에는 서버가 그 범위에서 고른
  작은 대표 evidence만 저장하며, 모델이 돌려준 원본 id는 쓰지 않는다.
- **취소·revision 확인**: 모델 호출 사이마다 잡이 여전히 유효한지 확인하고, 문서가
  바뀌면 중간 결과를 저장하지 않고 중단한다.
"""

import asyncio
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace

from sqlalchemy import func, select, update

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
    ProviderRequestBudget,
    current_provider_request_budget,
    new_provider_request_budget,
    provider_checkpoint_fingerprint,
    provider_request_budget_scope,
    providers_share_runtime_configuration,
)
from app.services.summary.schema import ArtifactDraft, bounded_evidence_chunk_ids
from app.services.summary.settings import (
    GROUP_SUMMARY_MAX_CHARS,
    PROMPT_VERSION,
    REDUCE_FAN_IN,
    SCHEMA_VERSION,
    SUMMARY_REQUEST_GUARD_TIMEOUT_SEC,
    summary_node_concurrency,
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

# 출력 길이 계약 위반(map_summary_too_long)에는 분할을 얕게만 시도한다.
#
# 컨텍스트 초과는 입력을 줄이면 반드시 풀리지만, 출력이 긴 것은 **입력 크기의 함수가
# 아니다** — 스키마 강제가 걸리지 않는 경로의 모델은 입력을 1/8로 줄여도 계속 넘긴다.
# 그런 모델에 깊이 3까지 재귀하면 그룹당 20회 넘는 호출을 태우고도 결국 실패한다.
# 한 번은 줄여서 기회를 주되, 그 다음은 모델이 준 출력을 잘라 쓰는 쪽이 낫다.
MAX_OUTPUT_LENGTH_SPLIT_DEPTH = 1

# 구조화 reduce가 컨텍스트를 넘을 때 그룹 요약을 한 단계 더 묶어 다시 시도하는 횟수.
# 2회면 마지막 레벨을 1/4 이하로 줄인다 — 그래도 안 되면 입력이 아니라 다른 문제다.
MAX_REDUCE_ADAPT = 2

# 청크 하나가 통째로 컨텍스트를 넘을 때 그 텍스트를 더 나눌지 판단하는 하한.
# 이보다 짧으면 나눠도 의미 있는 요약이 나오지 않는다.
MIN_TEXT_SPLIT_CHARS = 200


class SummaryCancelled(Exception):
    """잡이 취소·교체됨 — 실패로 기록하지 않고 조용히 종료한다."""


class SummaryRevisionChanged(Exception):
    """실행 중 문서가 바뀜 — 중간 결과를 저장하지 않는다."""


class SummaryConsentRevoked(Exception):
    """외부 전송 동의가 실행 중 철회됨 — 다음 요청을 보내지 않는다."""


class SummaryProviderChanged(Exception):
    """실행 중 공급자 설정이 바뀜 — 이전 설정 객체로 계속 보내지 않는다."""


class SummaryNoContent(Exception):
    """요약할 본문이 없다."""


@dataclass
class _NodeResult:
    """실행된(또는 재사용된) 노드 한 개."""

    node_id: uuid.UUID
    input_hash: str
    summary_text: str
    # coverage는 reduce/hash/revision 의미를 위해 현재 실행 동안 온전히 유지한다.
    # evidence만 DB와 최종 사용자 산출물에 들어가는 작은 대표 집합이다.
    coverage_chunk_ids: list[str]
    evidence_chunk_ids: list[str]
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
) -> Document:
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
    return doc


async def _assert_network_call_allowed(session, *, expected_provider, **guard_args) -> None:
    """실제 모델 호출 직전 최신 동의와 provider 설정까지 확인한다.

    로컬/외부 OpenAI 호환 공급자만 네트워크를 사용한다. 설정을 매번 다시 해석하므로
    endpoint/model/local-native/API 키가 실행 중 바뀌면 이미 만들어 둔 provider 객체로
    다음 청크를 보내지 않는다. API 키는 메모리 안에서만 비교하고 지문·로그에 넣지 않는다.
    """
    doc = await _assert_runnable(session, **guard_args)
    if getattr(expected_provider, "provider_name", "") != "openai_compatible":
        return

    if not getattr(expected_provider, "is_local", False):
        from app.models.user import User

        user = await session.get(User, doc.user_id)
        if user is None or not (doc.external_evidence_enabled and user.external_ai_allowed):
            raise SummaryConsentRevoked

    from app.services.summary.factory import get_summary_provider

    current_provider = await get_summary_provider(session, resolve_identity=True)
    if not providers_share_runtime_configuration(expected_provider, current_provider):
        raise SummaryProviderChanged


@dataclass
class _ProviderAttemptCounter:
    attempts: int = 0


async def _call_provider(
    call,
    argument,
    *,
    guard,
    request_budget: ProviderRequestBudget,
    attempt_counter: _ProviderAttemptCounter | None = None,
):
    """provider 메서드를 한 번 실행한다; actual HTTP retry는 공유 budget 안에서 수행된다.

    메서드 전체를 재시도하면 그 안의 출력 예산 확대·스키마 재질의도 처음부터 반복돼
    최대 12회로 증폭한다. OpenAI provider는 동일 payload 전송만 자체 재시도하고, 여기선
    deterministic/test provider에도 취소·revision 가드를 적용하는 역할만 맡는다.
    """
    if guard is not None:
        remaining = request_budget.remaining_seconds()
        if remaining <= 0:
            raise SummaryNetworkError("timeout", "node_deadline_exceeded")
        deadline = asyncio.timeout(remaining)
        try:
            async with deadline:
                await guard()
        except TimeoutError as exc:
            if not deadline.expired():
                raise
            raise SummaryNetworkError("timeout", "node_deadline_exceeded") from exc
    if attempt_counter is not None:
        attempt_counter.attempts += 1
    with provider_request_budget_scope(request_budget):
        return await asyncio.to_thread(call, argument)


def _run_request_guard_from_worker(loop, guard) -> None:
    """worker에서 async 전송 가드를 fail-closed bounded wait로 실행한다."""
    budget = current_provider_request_budget()
    remaining = budget.remaining_seconds() if budget is not None else None
    if remaining is not None and remaining <= 0:
        raise SummaryNetworkError("timeout", "node_deadline_exceeded")
    timeout = SUMMARY_REQUEST_GUARD_TIMEOUT_SEC
    deadline_limited = remaining is not None and remaining < timeout
    if remaining is not None:
        timeout = min(timeout, remaining)
    future = asyncio.run_coroutine_threadsafe(guard(), loop)
    try:
        future.result(timeout=timeout)
    except FutureTimeoutError:
        # guard coroutine 자체가 TimeoutError를 올린 경우와 result() 대기 timeout을
        # 구분한다. 완료된 future의 예외는 원형 그대로 전파한다.
        if future.done():
            future.result()
        future.cancel()
        reason = "node_deadline_exceeded" if deadline_limited else "pre_request_guard_timeout"
        raise SummaryNetworkError("timeout", reason) from None


async def _find_reusable(session, document_id: uuid.UUID, input_hash: str) -> SummaryNode | None:
    """같은 문서에서 같은 입력으로 이미 성공한 노드를 찾는다."""
    return (
        (
            await session.execute(
                select(SummaryNode)
                .where(
                    SummaryNode.document_id == document_id,
                    SummaryNode.input_hash == input_hash,
                    SummaryNode.status == "succeeded",
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


def _output_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _representative_error(group: BaseExceptionGroup) -> BaseException:
    """여러 노드가 함께 실패했을 때 호출자에게 올릴 예외 하나를 고른다.

    취소·revision 변경은 실패가 아니라 제어 흐름이다. 모델 실패를 올리면 사용자가 직접
    취소한 작업이 '요약 실패'로 기록되고, 문서가 바뀌어 버려야 할 결과가 공급자 오류로
    둔갑해 오류 리포트를 오염시킨다. 그래서 하나라도 있으면 그쪽을 우선한다.

    형제 태스크를 취소하며 생긴 `CancelledError`는 원인이 아니므로 제외한다 — 그걸
    올리면 진짜 실패 원인이 분류 단계(`_classify_pipeline_failure`)에 닿지 못한다.
    """
    leaves: list[BaseException] = []
    pending: list[BaseException] = [group]
    while pending:
        exc = pending.pop(0)
        if isinstance(exc, BaseExceptionGroup):
            pending = list(exc.exceptions) + pending
        elif not isinstance(exc, asyncio.CancelledError):
            leaves.append(exc)
    for exc in leaves:
        if isinstance(
            exc,
            SummaryCancelled
            | SummaryRevisionChanged
            | SummaryConsentRevoked
            | SummaryProviderChanged,
        ):
            return exc
    # 남은 게 없으면 실행 자체가 밖에서 취소된 것이다 — 그대로 전파한다.
    return leaves[0] if leaves else asyncio.CancelledError()


async def _run_level(factory, specs: list[dict]) -> list[_NodeResult]:
    """같은 레벨의 노드들을 동시에 실행한다. 결과는 입력(=position) 순서를 유지한다.

    한 레벨 안의 노드는 서로의 결과를 쓰지 않으므로 순서를 지킬 이유가 없고, 순차
    실행은 곧 대기 시간이다(실기기: map 794개 x 노드당 수 초 = 수 시간).

    완료 순서가 아니라 **입력 순서**로 결과를 모으는 것이 핵심이다. 순서가 실행마다
    달라지면 상위 reduce의 input_hash가 같이 달라져 노드 재사용이 통째로 무효가 되고,
    같은 문서를 재시도할 때마다 처음부터 다시 요약하게 된다.
    """
    concurrency = max(1, summary_node_concurrency())
    if concurrency == 1 or len(specs) <= 1:
        return [await _process_node(factory, **spec) for spec in specs]

    results: list[_NodeResult | None] = [None] * len(specs)
    limit = asyncio.Semaphore(concurrency)

    async def run_one(index: int, spec: dict) -> None:
        async with limit:
            results[index] = await _process_node(factory, **spec)

    try:
        # TaskGroup은 첫 실패에서 나머지를 취소한다. gather는 취소하지 않아, 사용자가
        # 이미 오류를 본 뒤에도 남은 수백 개 호출이 계속 모델을 돌린다.
        async with asyncio.TaskGroup() as tasks:
            for index, spec in enumerate(specs):
                tasks.create_task(run_one(index, spec))
    except BaseExceptionGroup as group:
        raise _representative_error(group) from None

    if any(node is None for node in results):
        # TaskGroup은 실패하면 예외를 던지므로 여기 도달하면 계약이 깨진 것이다. 조용히
        # 건너뛰면 그 그룹의 내용이 통째로 빠진 요약이 '정상 완료'로 저장된다.
        raise SummaryNetworkError("bad_response", "level_incomplete")
    return [node for node in results if node is not None]


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
        provider_fingerprint=provider_checkpoint_fingerprint(provider),
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

    async def _network_guard() -> None:
        async with factory() as session:
            await _assert_network_call_allowed(
                session, expected_provider=provider, check_full_hash=False, **guard_args
            )

    # OpenAICompatibleSummaryProvider는 공개 메서드 한 번 안에서도 출력 예산 확대 등으로
    # HTTP 요청을 여러 번 보낼 수 있다. worker thread에서 실제 전송 직전 main loop의
    # async DB 가드를 실행해, 그 내부 재요청 사이에 동의/설정이 바뀐 경우도 차단한다.
    install_request_guard = getattr(provider, "set_before_request_guard", None)
    if callable(install_request_guard):
        loop = asyncio.get_running_loop()

        def _guard_from_worker() -> None:
            _run_request_guard_from_worker(loop, _network_guard)

        install_request_guard(_guard_from_worker)
    counter = _Counter()

    # --- 레벨 0: 청크 그룹 map ---
    level_nodes = await _run_level(
        factory,
        [
            dict(
                provider=provider,
                run_id=run_id,
                document_id=document_id,
                level=0,
                position=position,
                input_hash=map_node_input_hash(
                    context_key, [(c.chunk_id, c.text) for c in group.chunks]
                ),
                request=GroupRequest(
                    group_id=group.group_id,
                    section_title=group.section_title,
                    chunks=group.chunks,
                    learner_level=learner_level,
                    language=language,
                ),
                source_ids=list(dict.fromkeys(c.chunk_id for c in group.chunks)),
                # 레벨 0의 요청 청크는 원본 청크 그 자체다.
                chunk_sources={c.chunk_id: [c.chunk_id] for c in group.chunks},
                section_title=group.section_title,
                counter=counter,
                guard_args=guard_args,
            )
            for position, group in enumerate(groups)
        ],
    )

    # --- 레벨 1+: bounded fan-in reduce ---
    level = 1

    def _reduce_spec(position: int, batch: list[_NodeResult], level: int) -> dict:
        # coverage는 자식 전체 범위의 합집합 — evidence로 줄이면 상위 hash/출처 의미가
        # 매 단계마다 소실된다. 작은 대표 집합은 저장 직전에 별도로 계산한다.
        merged_source_ids: list[str] = []
        seen_source_ids: set[str] = set()
        for child in batch:
            for cid in child.coverage_chunk_ids:
                if cid not in seen_source_ids:
                    seen_source_ids.add(cid)
                    merged_source_ids.append(cid)
        return dict(
            provider=provider,
            run_id=run_id,
            document_id=document_id,
            level=level,
            position=position,
            input_hash=reduce_node_input_hash(
                context_key, level, [(n.input_hash, n.summary_text) for n in batch]
            ),
            request=GroupRequest(
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
            ),
            source_ids=merged_source_ids,
            # 레벨 1+의 요청 청크는 자식 노드다 — 그 노드의 출처가 원본 chunk id다.
            chunk_sources={
                str(child.node_id): list(child.coverage_chunk_ids) for child in batch
            },
            section_title=batch[0].section_title,
            counter=counter,
            guard_args=guard_args,
        )

    async def reduce_round(nodes: list[_NodeResult], fan_in: int, level: int):
        """한 레벨을 fan_in개씩 묶어 상위 노드로 줄인다.

        레벨과 레벨 사이에는 의존이 있지만 한 레벨 안의 배치끼리는 독립이므로 동시에
        실행한다(레벨 경계는 그대로 지킨다 — 자식 요약이 있어야 부모를 만들 수 있다).
        """
        return await _run_level(
            factory,
            [
                _reduce_spec(position, batch, level)
                for position, batch in enumerate(fan_in_batches(nodes, fan_in))
            ],
        )

    # 섹션은 최상위가 아니라 **마지막으로 접히기 직전 레벨**에서 뽑는다.
    #
    # 최상위 레벨은 정의상 REDUCE_FAN_IN개 이하다. 거기서 섹션을 만들면 1,060페이지
    # 문서도 10페이지 문서도 똑같이 최대 8개 섹션을 받고, 구조화 요약이 참고하는 원문은
    # 8 x GROUP_SUMMARY_MAX_CHARS로 고정된다(실기기: 16,226청크 문서가 섹션 4개·580자로
    # 끝났다). 축약 트리는 문서가 클수록 깊어지므로 최상위만 보면 **큰 문서일수록 요약이
    # 짧아지는** 역전이 생긴다.
    #
    # 직전 레벨은 자연스럽게 유계다: 마지막 reduce가 그걸 fan-in개 이하로 접었으므로
    # 노드 수는 REDUCE_FAN_IN^2(=64)를 넘지 않는다. 별도 상한이 필요 없다.
    section_nodes = level_nodes
    while len(level_nodes) > REDUCE_FAN_IN:
        section_nodes = level_nodes
        level_nodes = await reduce_round(level_nodes, REDUCE_FAN_IN, level)
        level += 1

    # --- 구조화 reduce: 마지막 레벨(≤ fan-in)을 한 번에 문서 요약으로 ---
    async with factory() as session:
        await _assert_runnable(session, check_full_hash=True, **guard_args)

    def as_group_summaries(nodes: list[_NodeResult]) -> list[GroupSummary]:
        return [
            GroupSummary(
                group_id=f"g{i}",
                section_title=n.section_title,
                summary_text=n.summary_text,
                source_chunk_ids=n.coverage_chunk_ids,
            )
            for i, n in enumerate(nodes)
        ]

    # 구조화 reduce도 입력이 컨텍스트를 넘을 수 있다. 여기서 적응하지 않으면 수백 개
    # map 노드를 몇 시간에 걸쳐 다 만든 뒤 마지막 한 번에 실패해 artifact가 0개로 끝나고,
    # 재시도해도 map은 재사용돼 곧바로 같은 호출로 돌아가 영구 실패한다.
    structured: dict | None = None
    structured_request_budget = new_provider_request_budget()
    for attempt in range(MAX_REDUCE_ADAPT + 1):
        try:
            structured = await _call_provider(
                provider.summarize_document,
                DocumentRequest(
                    group_summaries=as_group_summaries(level_nodes),
                    learner_level=learner_level,
                    language=language,
                    # 섹션은 아래에서 section_nodes로 통째로 덮어쓴다 — 모델에게
                    # 요구하지 않는다. 요구하면 매 호출 버릴 출력을 생성해 reduce 출력
                    # 예산을 먹고(절단 위험을 키우고), 그 안의 group id 하나가 어긋나면
                    # reduce_group_ids_unknown으로 저장되지도 않는 값 때문에 문서 전체
                    # 요약이 영구 실패한다.
                    include_sections=False,
                    include_prerequisites=include_prerequisites,
                ),
                guard=_network_guard,
                request_budget=structured_request_budget,
            )
            break
        except Exception as exc:
            too_large = _is_input_too_large(exc)
            logger.warning(
                "summary_node_failed",
                document_id=str(document_id),
                node_level=-1,  # -1 = 구조화 reduce(계층 레벨이 아니다)
                position=0,
                completed_before=counter.processed,
                input_chunks=len(level_nodes),
                input_chars=sum(len(n.summary_text) for n in level_nodes),
                error_type=type(exc).__name__,
                failure_category=getattr(exc, "category", None) or "unexpected",
                failure_reason=getattr(exc, "reason", None) or "none",
            )
            if not too_large or attempt >= MAX_REDUCE_ADAPT or len(level_nodes) < 2:
                raise
            # 입력을 더 줄인다 — 한 단계 더 묶어 그룹 요약 개수를 절반 이하로 만든다.
            logger.info(
                "summary_reduce_collapse_retry",
                document_id=str(document_id),
                groups=len(level_nodes),
                failure_reason=getattr(exc, "reason", None) or "none",
            )
            level_nodes = await reduce_round(
                level_nodes, max(2, (len(level_nodes) + 1) // 2), level
            )
            level += 1
    if structured is None:  # 위 루프는 성공하거나 raise한다 — 도달하면 계약이 깨진 것이다
        raise SummaryNetworkError("bad_response", "reduce_no_result")

    # 섹션은 모델 출력이 아니라 직전 레벨 노드에서 결정론적으로 만든다.
    #
    # 모델에 그 노드들을 다 넣어 섹션을 받아내려면 입력이 컨텍스트를 넘는다(예: 27노드
    # x 400자 ≈ 8,500토큰 > LOCAL_NUM_CTX). 노드는 이미 요약문·제목·출처를 들고 있으므로
    # 모델을 한 번 더 부를 이유가 없다. 출처도 서버가 계산한 값을 그대로 쓴다.
    if include_sections:
        structured["sections"] = [
            {
                "title": node.section_title,
                "summary": node.summary_text,
                "sourceChunkIds": node.evidence_chunk_ids,
            }
            for node in section_nodes
            if node.summary_text and node.evidence_chunk_ids
        ]
        logger.info(
            "summary_sections_from_level",
            document_id=str(document_id),
            sections=len(structured["sections"]),
            top_level_nodes=len(level_nodes),
        )
    # 스레드로 보낸다. 이 안에서 문서 전체 텍스트에 정규식 6종을 finditer로 훑고
    # 모든 청크를 문장 단위로 쪼개 다시 정규식을 돌린다 — 1,060쪽·16,226청크 교재면
    # 수 초가 걸리고, 그동안 이벤트 루프가 통째로 멈춘다. 같은 시각 열려 있는 Q&A
    # 스트림이 heartbeat를 못 보내 클라이언트에서 연결 끊김으로 처리되고, 진행 중이던
    # 답변이 중단된 것으로 표시된다.
    return await asyncio.to_thread(
        finalize_artifacts,
        structured,
        lookup,
        learner_level=learner_level,
        include_sections=include_sections,
        include_prerequisites=include_prerequisites,
    )


class _Counter:
    """호출 사이 전체 해시 확인 주기를 세는 카운터."""

    def __init__(self) -> None:
        self.processed = 0  # 완료된 노드 수 — 실패 로그의 `completed_before`에 쓴다
        self.started = 0  # 시작된 노드 수 — 전체 해시 확인 주기의 기준

    def next_needs_full_check(self) -> bool:
        """N개마다 한 번 True.

        완료 수(`processed`)가 아니라 시작 순번을 쓴다. 노드를 동시에 실행하면 아직
        아무것도 끝나지 않은 시점에 여러 노드가 동시에 진입해 완료 수가 전부 0이고,
        그 전부가 비싼 전체 해시 재계산을 함께 돌린다.

        증가와 읽기 사이에 await가 없으므로 같은 이벤트 루프의 태스크끼리는 서로
        끼어들지 못한다 — 각 호출이 서로 다른 순번을 받는다.
        """
        index = self.started
        self.started += 1
        return index % FULL_HASH_CHECK_INTERVAL == 0


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

    `source_ids`와 `chunk_sources`는 현재 실행이 계산한 전체 coverage다. 재사용 노드도
    DB에 저장된 evidence-only 배열을 읽어 coverage로 승격하지 않는다. 레벨 0은 자기
    자신, 레벨 1+는 자식 노드의 coverage이며, 분할·열화로 버린 조각은 제외한다.
    """
    async with factory() as session:
        await _assert_runnable(
            session, check_full_hash=counter.next_needs_full_check(), **guard_args
        )
        reusable = await _find_reusable(session, document_id, input_hash)
        if reusable is not None:
            evidence_ids = bounded_evidence_chunk_ids(source_ids)
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
                source_chunk_ids_json=evidence_ids,
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
                # 과거 노드의 UUID를 복사하지 않는다. 같은 입력 hash를 재사용하더라도
                # coverage/evidence는 이번 실행이 계산한 현재 id에서 다시 만든다.
                coverage_chunk_ids=list(source_ids),
                evidence_chunk_ids=evidence_ids,
                section_title=section_title,
            )

    async def _guard() -> None:
        async with factory() as session:
            await _assert_network_call_allowed(
                session,
                expected_provider=provider,
                check_full_hash=False,
                **guard_args,
            )

    # 모델 호출은 트랜잭션 밖에서 한다 — writer 락을 붙잡은 채 네트워크를 기다리지 않는다.
    attempt_counter = _ProviderAttemptCounter()
    request_budget = new_provider_request_budget()
    try:
        result = await _summarize_adaptively(
            provider,
            request,
            document_id=document_id,
            guard=_guard,
            attempt_counter=attempt_counter,
            request_budget=request_budget,
        )
    except (
        SummaryCancelled,
        SummaryRevisionChanged,
        SummaryConsentRevoked,
        SummaryProviderChanged,
    ):
        # 취소·문서 변경은 정상 제어 흐름이다. 여기서 경고로 남기면 취소 한 건마다
        # "미분류 노드 실패" 경고가 쌓여, 실제 모델 실패를 찾을 때 구분되지 않는다.
        raise
    except Exception as exc:
        # 어느 노드에서 죽었는지 남긴다. 이게 없으면 "요약 실패"만 보이고 첫 호출에서
        # 실패했는지 수백 번째에서 실패했는지 구분할 수 없다(원인 범위가 완전히 다르다).
        logger.warning(
            "summary_node_failed",
            document_id=str(document_id),
            # `level`은 structlog의 add_log_level 프로세서가 무조건 "warning"으로 덮어써
            # 계층 레벨이 사라진다. 노드의 유일키가 (level, position)이라 그 값이 없으면
            # 어느 노드가 죽었는지 특정할 수 없다 — 이 로그를 넣은 목적 자체가 무효화된다.
            node_level=level,
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
    evidence_ids = bounded_evidence_chunk_ids(covered_ids)

    async def _persist() -> uuid.UUID:
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
                source_chunk_ids_json=evidence_ids,
                attempt_count=max(attempt_counter.attempts, request_budget.requests_started),
                reused=False,
            )
            session.add(node)
            await _bump_completed(session, run_id)
            await session.commit()
            await session.refresh(node)
            return node.id

    # 모델 호출이 끝난 결과는 취소되더라도 반드시 저장한다. 노드를 동시에 실행하면 형제
    # 노드 하나가 실패할 때 나머지가 취소되는데, 그 취소가 저장 직전에 닿으면 이미 지불한
    # 모델 호출이 통째로 버려진다. 그러면 재시도해도 재사용할 노드가 없어, 체크포인트를
    # 둔 이유(중단 후 재시도가 성공 노드를 재사용한다) 자체가 사라진다.
    persist_task = asyncio.create_task(_persist())
    try:
        node_id = await asyncio.shield(persist_task)
    except asyncio.CancelledError:
        # 형제 실패로 wrapper가 취소돼도 이미 모델 호출을 끝낸 결과의 commit이 끝날 때까지
        # 기다린다. task 자체는 shield돼 있으므로 취소하지 않고, 완료 후 원래 취소를 올린다.
        await persist_task
        raise
    counter.processed += 1
    return _NodeResult(
        node_id=node_id,
        input_hash=input_hash,
        summary_text=text,
        coverage_chunk_ids=list(covered_ids),
        evidence_chunk_ids=evidence_ids,
        section_title=section_title,
    )


def _resolve_covered(
    covered_request_ids: list[str], chunk_sources: dict[str, list[str]]
) -> list[str]:
    """요약에 반영된 요청 청크 id → 서버 소유 원본 chunk id(순서 유지·중복 제거)."""
    out: list[str] = []
    seen: set[str] = set()
    for cid in covered_request_ids:
        for sid in chunk_sources.get(cid, ()):
            if sid not in seen:
                seen.add(sid)
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

    `reduce_finish_length`도 포함한다. 구조화 reduce는 프롬프트 약 2,000토큰 + 출력
    4,096으로 이미 num_ctx(8192) 상한 근처라 map처럼 출력 예산을 키울 수 없다 —
    키우면 이번엔 컨텍스트가 넘친다. 절단됐을 때 남은 수단은 입력(그룹 요약 개수)을
    줄이는 것뿐이고, 실행기는 이미 그 경로(MAX_REDUCE_ADAPT)를 갖고 있다.

    한 번도 재시도하지 않은 날것의 `finish_length`는 **제외한다**. 공급자가 예산을 늘려
    다시 부르는 것이 먼저이고, 그 단계를 건너뛰고 나누면 호출만 증폭된다.
    """
    if not isinstance(exc, SummaryNetworkError):
        return False
    return exc.category == "context_overflow" or exc.reason in (
        "map_summary_too_long",
        "map_finish_length",
        "reduce_finish_length",
    )


def _salvageable_text(exc: Exception) -> str | None:
    """계약을 넘겼다는 이유로 거절된 출력 중, 잘라서라도 쓸 수 있는 값.

    분할이 통하지 않는 모델일 때의 바닥이다. 이 값이 없으면(공급자가 아무것도 주지
    못한 경우) 지어내지 않고 그대로 실패시킨다 — 실패 범주도 덮지 않는다.
    """
    if not isinstance(exc, SummaryNetworkError):
        return None
    text = (exc.oversized_text or "").strip()
    return text or None


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
    attempt_counter: _ProviderAttemptCounter | None = None,
    request_budget: ProviderRequestBudget | None = None,
    depth: int = 0,
) -> _AdaptiveResult:
    """그룹 요약. 입력이 모델 컨텍스트를 넘으면 절반으로 나눠 다시 시도한다.

    Ollama가 기기마다 다른 컨텍스트로 모델을 올리고 서버는 그 값을 알 수 없으므로,
    상한을 추측하는 대신 실패했을 때 적응한다. 나눈 요약들은 다시 한 번 합쳐 노드
    하나당 요약 하나라는 계약을 유지한다(출처는 호출자가 서버 소유로 계산하되,
    여기서 돌려주는 covered_ids 범위로 한정한다).
    """
    all_ids = [c.chunk_id for c in request.chunks]
    chunks = request.chunks
    if request_budget is None:
        request_budget = new_provider_request_budget()
    try:
        summary = await _call_provider(
            provider.summarize_group,
            request,
            guard=guard,
            request_budget=request_budget,
            attempt_counter=attempt_counter,
        )
        return _AdaptiveResult(text=summary.summary_text, degraded=False, covered_ids=all_ids)
    except Exception as exc:
        # 출력이 길어서 거절된 경우엔 얕게만 나눠 본다. 계속 넘기는 모델이면 더 나눠도
        # 같은 결과라, 남은 유일한 수단인 '잘라 쓰기'로 내려간다.
        salvage = _salvageable_text(exc)
        if salvage is not None and depth >= MAX_OUTPUT_LENGTH_SPLIT_DEPTH:
            logger.info(
                "summary_group_length_truncated",
                document_id=str(document_id),
                group_id=request.group_id,
                depth=depth,
                # 원문은 남기지 않는다 — 길이만.
                oversized_chars=len(salvage),
            )
            return _AdaptiveResult(
                text=_truncate_on_boundary(salvage), degraded=True, covered_ids=all_ids
            )
        if not _is_input_too_large(exc) or depth >= MAX_SPLIT_DEPTH:
            raise
        if len(chunks) < 2:
            # 청크 하나가 통째로 상한을 넘는다(build_groups는 긴 청크를 쪼개지 않는다).
            # 여기서 포기하면 그 청크 하나 때문에 문서 전체 요약이 실패한다 — 나머지
            # 99%가 요약 가능한데도. 텍스트를 나눈다(출처는 같은 chunk_id라 그대로다).
            halves = _split_chunk_text(chunks[0]) if chunks else None
            if halves is None:
                raise
            chunks = halves
        logger.info(
            "summary_group_split_retry",
            document_id=str(document_id),
            group_id=request.group_id,
            depth=depth,
            input_chunks=len(request.chunks),
            input_chars=sum(len(c.text) for c in request.chunks),
            failure_reason=getattr(exc, "reason", None) or "none",
        )

    mid = _split_point(chunks)
    parts: list[_AdaptiveResult] = []
    degraded = False
    last_failure: Exception | None = None
    for index, half in enumerate((chunks[:mid], chunks[mid:])):
        # 자식 실패를 여기서 잡지 않으면 아래 병합 폴백에 도달하지 못하고, 이미 비용을
        # 지불한 형제 부분 요약까지 함께 버려진다.
        try:
            child = await _summarize_adaptively(
                provider,
                replace(request, group_id=f"{request.group_id}s{index}", chunks=half),
                document_id=document_id,
                guard=guard,
                attempt_counter=attempt_counter,
                request_budget=request_budget,
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
        # 조각이 전부 길이 계약 위반으로 거절됐다면 그 출력을 잘라서라도 남긴다.
        salvage = _salvageable_text(last_failure) if last_failure else None
        if salvage is not None:
            return _AdaptiveResult(
                text=_truncate_on_boundary(salvage), degraded=True, covered_ids=all_ids
            )
        # 어느 조각도 요약하지 못했고 건질 출력도 없다 — 지어내지 않고 실패로 올린다.
        # 원래 예외를 그대로 올린다: 범주를 context_overflow로 덮으면 출력 길이 계약
        # 위반 같은 메모리와 무관한 실패에 "메모리를 확보하라"는 실행 불가 안내가 뜨고,
        # 로그의 실제 failure_reason도 사라진다.
        raise last_failure or SummaryNetworkError("context_overflow", "map_context_overflow")

    covered = _merge_ids(parts)
    if len(parts) == 1:
        # 합칠 대상이 하나뿐이다. 이미 400자로 압축된 요약을 다시 압축하는 순손실
        # 호출이고, 그 호출이 초과가 아닌 이유로 실패하면 살아남은 유일한 조각까지
        # 버려 노드가 통째로 실패한다.
        return _AdaptiveResult(text=parts[0].text, degraded=degraded, covered_ids=covered)

    # 나눈 요약을 다시 하나로 — 입력이 훨씬 짧아 같은 계약으로 통과한다.
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
        summary = await _call_provider(
            provider.summarize_group,
            merged,
            guard=guard,
            request_budget=request_budget,
            attempt_counter=attempt_counter,
        )
        return _AdaptiveResult(text=summary.summary_text, degraded=degraded, covered_ids=covered)
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


def _split_chunk_text(chunk: ChunkInput) -> list[ChunkInput] | None:
    """청크 하나의 텍스트를 둘로 나눈다. 더 나눌 수 없으면 None.

    chunk_id를 그대로 물려주므로 출처는 변하지 않는다 — 두 조각 모두 같은 청크를
    가리키고, covered_ids 합집합에서 자연히 하나로 합쳐진다.
    """
    text = chunk.text
    if len(text) < 2 * MIN_TEXT_SPLIT_CHARS:
        return None
    mid = len(text) // 2
    space = text.rfind(" ", 0, mid)
    cut = space if space > mid // 2 else mid  # 경계에서 자르되 너무 앞으로 가지 않는다
    first, second = text[:cut].strip(), text[cut:].strip()
    if not first or not second:
        return None
    return [replace(chunk, text=first), replace(chunk, text=second)]


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
    """계약 길이로 자르되 **문장 경계**까지만 남긴다.

    예전에는 마지막 공백에서 잘랐다. 의료 문장에서 그건 뜻을 뒤집는다 —
    "이 약은 임신부에게 금기가 아니다"가 "이 약은 임신부에게 금기가"로 잘리면
    정반대를 읽게 된다. 같은 위험 때문에 schema.py의 `_clean_text`가 이미 문장
    경계 절단으로 옮겼는데, 저장 경로인 여기만 공백 절단으로 남아 있었다. 규칙이
    두 벌이면 다음 사람이 어느 쪽을 따라야 하는지 알 수 없다.

    문장 경계를 하나도 못 찾으면 `_clean_text`는 통째로 버린다. 여기서는 버릴 수
    없다 — 이건 이미 실패한 요약에서 그나마 건진 텍스트라, 버리면 그 그룹은 아무
    내용도 남기지 못한다. 그래서 경계가 없을 때만 길이로 자른다.
    """
    from app.services.summary.schema import _clean_text

    if len(text) <= GROUP_SUMMARY_MAX_CHARS:
        return text
    kept = _clean_text(text, GROUP_SUMMARY_MAX_CHARS, field_name="group_summary_salvage")
    return kept or text[:GROUP_SUMMARY_MAX_CHARS].rstrip()


async def _bump_completed(session, run_id: uuid.UUID) -> None:
    """진행률 카운터를 1 올린다.

    읽고-더하고-쓰는 대신 DB에서 증가시킨다. 노드를 동시에 실행하면 세션 두 개가 같은
    값을 읽고 같은 값을 써서 증가분을 잃고, 진행률이 실제보다 낮은 값에서 멈춘다.
    """
    await session.execute(
        update(SummaryRun)
        .where(SummaryRun.id == run_id)
        .values(completed_nodes=func.coalesce(SummaryRun.completed_nodes, 0) + 1)
    )
