"""요약 공급자 인터페이스 — EmbeddingProvider와 완전히 분리된다.

공급자는 chunk id + 텍스트 + section_title만 본다. page/bbox 출처는 절대 다루지 않는다
(서버가 chunk의 저장된 source_refs에서 재구성한다). 실제 API 키·endpoint·모델명을
코드에 하드코딩하지 않는다.
"""

import hashlib
import hmac
import json
import math
import random
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol

from app.core.logging import get_logger

# finish_reason/done_reason 분류값은 endpoint.parse_chat_content와 공유한다.
from app.services.summary.endpoint import KNOWN_FINISH_REASONS as _KNOWN_FINISH_REASONS
from app.services.summary.settings import (
    GROUP_SUMMARY_MAX_CHARS,
    SUMMARY_MAP_MAX_TOKENS,
    SUMMARY_MAP_RETRY_MAX_TOKENS,
    SUMMARY_REDUCE_MAX_TOKENS,
)

logger = get_logger(__name__)

# 프롬프트가 잘렸을 때 모델이 낼 수 있어야 하는 대안 형태. 이걸 스키마에서 배제하면
# 잘린 프롬프트가 "정상 요약"으로 통과해 버린다(실측: strict 스키마 + num_ctx 부족 →
# 키=['summary'], anyOf 허용 → 키=['error']). 구조 강제와 초과 감지를 함께 얻는다.
_ERROR_ALTERNATIVE = {
    "type": "object",
    "properties": {"error": {"type": "string"}},
    "required": ["error"],
}


def _map_schema(*, allow_error: bool = True) -> dict:
    """map 응답 구조 — 정상 요약 또는 모델이 낸 오류 객체.

    `allow_error=False`는 오류 형태를 문법에서 빼 모델이 요약을 **반드시** 쓰게 한다.
    error 대안은 잘린 프롬프트를 감지하려고 둔 것인데, 초과와 무관한 청크에서도
    모델이 가장 쉬운 경로로 그것을 골라 버리면(스키마 이전에는 어떻게든 요약을 만들었다)
    그 노드가 즉시 실패해 문서 전체가 영구 실패한다. 초과 징후가 없는 error에는 이
    엄격한 문법으로 한 번 더 물어본다.
    """
    success = {
        "type": "object",
        "properties": {"summary": _text()},
        "required": ["summary"],
    }
    if not allow_error:
        return success
    return {"anyOf": [success, _ERROR_ALTERNATIVE]}


def _sourced(properties: dict, required: list[str]) -> dict:
    """모든 항목은 출처(group id)를 최소 1개 달고 나와야 한다.

    minItems가 없으면 문법이 빈 배열을 허용하는데, 서버는 빈 배열을 계약 위반으로
    보고 문서 전체를 실패시킨다 — 스키마와 서버 검증이 어긋나면 안 된다.
    """
    return {
        "type": "object",
        "properties": {
            **properties,
            "sourceGroupIds": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
        },
        "required": [*required, "sourceGroupIds"],
    }


def _text() -> dict:
    """문자열 필드 — 길이 상한은 **문법에 넣지 않는다**.

    Ollama(llama.cpp)의 JSON schema → GBNF 변환은 maxLength를 "N자까지 쓰고 나면 닫는
    따옴표만 허용"하는 문법으로 만든다. 즉 계약 위반을 **거부**하는 게 아니라 정확히
    N번째 글자에서 문자열을 **강제로 닫는다**. 모델이 상한보다 길게 쓰려 하면
    `"…투여 금기다"`가 `"…투여 금"`으로 문장 중간에서 잘린 채 문법상 유효한 JSON이
    되어 서버 길이 검증도 통과하고 그대로 사용자에게 노출된다(의미가 뒤집힐 수 있다).

    게다가 map 요약에서는 그 절단 때문에 `map_summary_too_long`이 영영 발생하지 않아,
    executor가 그것을 근거로 삼는 적응 분할이 native 경로에서 도달 불가능해진다.

    길이는 서버가 검증한다 — 초과 항목은 build_artifacts가 로그를 남기고 버리고,
    map 요약 초과는 분할 재시도의 신호가 된다. 큰 maxLength가 GBNF 변환 자체를
    깨뜨리는 문제(실측 2000 → "failed to parse grammar" 400)도 함께 사라진다.
    """
    return {"type": "string"}


def _document_schema(*, include_sections: bool, include_prerequisites: bool) -> dict:
    """구조화 reduce 응답 구조.

    스키마를 강제하지 않으면 로컬 모델이 overview를 문자열로 돌려주거나 sourceGroupIds를
    빠뜨려 서버가 항목을 통째로 버린다(실측). 강제하면 계약대로 나온다. 다만 잘린 프롬프트
    신호까지 막지 않도록 오류 객체 형태는 대안으로 남긴다.
    """
    props: dict = {
        "overview": _sourced({"text": _text()}, ["text"]),
        "keyConcepts": {
            "type": "array",
            "items": _sourced(
                {"term": _text(), "explanation": _text()},
                ["term", "explanation"],
            ),
        },
        "learnerExplanations": {
            "type": "array",
            "items": _sourced(
                {"level": _text(), "text": _text()},
                ["level", "text"],
            ),
        },
    }
    required = ["overview"]
    if include_sections:
        props["sections"] = {
            "type": "array",
            "items": _sourced(
                {"title": _text(), "summary": _text()},
                ["title", "summary"],
            ),
        }
        required.append("sections")
    if include_prerequisites:
        props["prerequisites"] = {
            "type": "array",
            "items": _sourced(
                {
                    "concept": _text(),
                    "whyNeeded": _text(),
                    "sourceType": {"type": "string"},
                },
                ["concept", "whyNeeded"],
            ),
        }
    return {
        "anyOf": [
            {"type": "object", "properties": props, "required": required},
            _ERROR_ALTERNATIVE,
        ]
    }


# 저장 가능한 내용을 담을 수 있는 reduce 응답의 배열 필드.
_CONTENT_LISTS = ("sections", "keyConcepts", "prerequisites", "learnerExplanations")


def _is_storable(item) -> bool:
    """저장 계층이 실제로 남길 항목인가 — 내용과 출처가 **둘 다** 있어야 한다.

    build_artifacts는 출처 없는 항목을 통째로 버린다. 여기서 "dict가 하나라도 있으면
    쓸 만하다"고 세면, 출처 없는 항목만 담긴 응답이 게이트를 통과해 artifact가 0개인
    요약이 SUCCEEDED로 저장된다(사용자에게는 '아직 요약을 만들지 않았어요'만 보인다).
    """
    if not isinstance(item, dict):
        return False
    sources = item.get("sourceGroupIds")
    if not isinstance(sources, list) or not sources:
        return False
    return any(
        isinstance(value, str) and value.strip()
        for key, value in item.items()
        if key != "sourceGroupIds"
    )


def _has_usable_content(parsed: dict) -> bool:
    """이 응답으로 artifact를 하나라도 만들 수 있는가.

    overview는 `{"text": ...}` 객체여야 한다 — 문자열로 온 overview는 build_artifacts가
    통째로 버리므로 "있다"고 세면 안 된다. overview가 없어도 다른 항목이 있으면 그것으로
    요약을 만든다(부분 응답을 버리지 않는다).
    """
    if _is_storable(parsed.get("overview")):
        return True
    return any(
        isinstance(parsed.get(key), list) and any(_is_storable(i) for i in parsed[key])
        for key in _CONTENT_LISTS
    )


def _looks_like_context_overflow(parsed: dict) -> bool:
    """계약 필드 대신 error 객체가 온 경우 — 입력 초과인지 판별한다.

    Ollama는 프롬프트를 조용히 자르므로 HTTP는 200이고 usage도 잘린 값이 온다. 유일한
    단서가 모델이 낸 error 문구뿐이라 여기서만 본문을 들여다본다(로그에는 남기지 않는다).
    """
    from app.services.summary.endpoint import MODEL_CONTEXT_OVERFLOW_HINTS

    error = parsed.get("error")
    if not isinstance(error, str):
        return False
    lowered = error.lower()
    return any(hint in lowered for hint in MODEL_CONTEXT_OVERFLOW_HINTS)


@dataclass
class ChunkInput:
    """공급자에게 전달하는 청크 — bbox·페이지 좌표는 포함하지 않는다."""

    chunk_id: str
    section_title: str | None
    text: str
    page_start: int
    page_end: int


@dataclass
class GroupRequest:
    group_id: str
    section_title: str | None
    chunks: list[ChunkInput]
    learner_level: str
    language: str


@dataclass
class GroupSummary:
    group_id: str
    section_title: str | None
    summary_text: str
    source_chunk_ids: list[str]


@dataclass
class DocumentRequest:
    group_summaries: list[GroupSummary]
    learner_level: str
    language: str
    include_sections: bool = True
    include_prerequisites: bool = True
    options: dict = field(default_factory=dict)


class SummaryProvider(Protocol):
    provider_name: str
    model_name: str
    available: bool
    supports_structured_output: bool

    def summarize_group(self, request: GroupRequest) -> GroupSummary: ...

    def summarize_document(self, request: DocumentRequest) -> dict: ...


@dataclass
class ProviderRequestBudget:
    """한 executor 노드가 공유하는 실제 HTTP 요청 수와 wall-clock deadline."""

    request_limit: int
    total_deadline_seconds: float
    clock: Callable[[], float] = time.monotonic
    requests_started: int = 0
    _started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self._started_at = self.clock()

    def remaining_seconds(self) -> float:
        return max(0.0, self.total_deadline_seconds - (self.clock() - self._started_at))

    def claim_request(self, per_request_timeout: float) -> float:
        from app.services.summary.endpoint import SummaryNetworkError

        if self.requests_started >= self.request_limit:
            raise SummaryNetworkError("timeout", "node_request_budget_exhausted")
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise SummaryNetworkError("timeout", "node_deadline_exceeded")
        self.requests_started += 1
        return min(per_request_timeout, remaining)


def new_provider_request_budget(
    *,
    total_deadline_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ProviderRequestBudget:
    from app.services.summary.settings import (
        SUMMARY_NODE_DEADLINE_SEC,
        SUMMARY_NODE_REQUEST_BUDGET,
    )

    return ProviderRequestBudget(
        request_limit=SUMMARY_NODE_REQUEST_BUDGET,
        total_deadline_seconds=(
            SUMMARY_NODE_DEADLINE_SEC
            if total_deadline_seconds is None
            else total_deadline_seconds
        ),
        clock=clock,
    )


_ACTIVE_REQUEST_BUDGET: ContextVar[ProviderRequestBudget | None] = ContextVar(
    "summary_provider_request_budget", default=None
)


@contextmanager
def provider_request_budget_scope(budget: ProviderRequestBudget):
    """asyncio.to_thread가 복사하는 context에 노드 budget을 설치한다."""
    token = _ACTIVE_REQUEST_BUDGET.set(budget)
    try:
        yield
    finally:
        _ACTIVE_REQUEST_BUDGET.reset(token)


def current_provider_request_budget() -> ProviderRequestBudget | None:
    """현재 worker context의 노드 budget(실제 전송 guard의 deadline 계산용)."""
    return _ACTIVE_REQUEST_BUDGET.get()


def _sleep_before_request_retry(delay: float) -> None:
    """테스트에서 실제 대기 없이 관찰할 수 있는 동기 backoff 경계."""
    time.sleep(delay)


def _retry_jitter() -> float:
    from app.services.summary.settings import SUMMARY_RETRY_JITTER_SEC

    return random.uniform(0.0, SUMMARY_RETRY_JITTER_SEC)


def _retry_delay(exc, retry_index: int) -> float:
    from app.services.summary.settings import (
        SUMMARY_RETRY_AFTER_CAP_SEC,
        SUMMARY_RETRY_BASE_DELAY_SEC,
        SUMMARY_RETRY_DELAY_CAP_SEC,
    )

    retry_after = getattr(exc, "retry_after_seconds", None)
    if exc.category == "rate_limited" and isinstance(retry_after, int | float):
        retry_after_value = float(retry_after)
        if math.isfinite(retry_after_value):
            return min(max(0.0, retry_after_value), SUMMARY_RETRY_AFTER_CAP_SEC)
    exponential = SUMMARY_RETRY_BASE_DELAY_SEC * (2**retry_index)
    return min(exponential + _retry_jitter(), SUMMARY_RETRY_DELAY_CAP_SEC)


def provider_checkpoint_fingerprint(
    provider: SummaryProvider, *, include_fail_safe_generation: bool = True
) -> str:
    """체크포인트 재사용에 쓰는 비밀 없는 공급자 지문.

    같은 ``provider_name``/``model_name``이어도 endpoint, 로컬 여부, native 전송 모드,
    모델 digest와 생성 설정이 다르면 결과를 섞어 쓰면 안 된다. endpoint 원문은 DB의
    input hash나 로그에서 역으로 노출되지 않도록 SHA-256만 포함하고 API 키는 어떤
    형태로도 포함하지 않는다.

    로컬 모델 카탈로그의 ``model_digest``와 외부 키의 설치별 HMAC identity를 실제
    provider 속성으로 전달한다. digest 조회가 실패한 로컬 실행은 일회성 generation을
    포함해 과거 checkpoint를 fail-safe로 재사용하지 않는다.
    """
    from app.services.summary.settings import (
        LOCAL_KEEP_ALIVE,
        LOCAL_MAX_TOKENS,
        LOCAL_NUM_CTX,
        LOCAL_REASONING_EFFORT,
    )

    provider_name = str(getattr(provider, "provider_name", ""))
    endpoint = str(getattr(provider, "_endpoint", "") or "").strip().rstrip("/")
    is_local = bool(getattr(provider, "is_local", False))
    if endpoint and provider_name == "openai_compatible":
        from app.services.summary.endpoint import SummaryNetworkError, validate_endpoint

        try:
            endpoint = validate_endpoint(endpoint, is_local=is_local)
        except SummaryNetworkError:
            # 런타임 설정 저장 경로는 이미 검증한다. 직접 만든 테스트 provider가 잘못된
            # 주소를 가졌더라도 지문 계산이 원래 오류보다 먼저 실패하지 않게 원문을 쓴다.
            pass
    uses_native = False
    if provider_name == "openai_compatible":
        try:
            uses_native = bool(getattr(provider, "uses_ollama_native", False))
        except Exception:  # 잘못된 테스트 설정도 지문 계산 자체를 깨뜨리지는 않는다.
            uses_native = False

    model_digest = getattr(provider, "model_digest", None)
    if not isinstance(model_digest, str) or not model_digest.strip():
        model_digest = None
    provider_identity_digest = getattr(provider, "provider_identity_digest", None)
    if not isinstance(provider_identity_digest, str) or not provider_identity_digest.strip():
        provider_identity_digest = None
    provider_identity_generation = getattr(provider, "provider_identity_generation", None)
    if (
        not isinstance(provider_identity_generation, str)
        or not provider_identity_generation.strip()
    ):
        provider_identity_generation = None

    generation: dict[str, object] = {
        "map_max_tokens": SUMMARY_MAP_MAX_TOKENS,
        "map_retry_max_tokens": SUMMARY_MAP_RETRY_MAX_TOKENS,
        "reduce_max_tokens": SUMMARY_REDUCE_MAX_TOKENS,
    }
    if provider_name == "openai_compatible":
        generation.update(
            {
                "temperature": 0 if is_local else 0.2,
                "local_max_tokens": LOCAL_MAX_TOKENS if is_local else None,
                "reasoning_effort": LOCAL_REASONING_EFFORT if is_local else None,
                "num_ctx": LOCAL_NUM_CTX if uses_native else None,
                "keep_alive": LOCAL_KEEP_ALIVE if uses_native else None,
            }
        )

    canonical = json.dumps(
        {
            "provider": provider_name,
            "model": str(getattr(provider, "model_name", "")),
            "model_digest": model_digest,
            # 외부 API 키의 설치별 HMAC. 키·무염 키 해시는 포함하지 않는다.
            "provider_identity_digest": provider_identity_digest,
            "provider_identity_generation": (
                provider_identity_generation if include_fail_safe_generation else None
            ),
            "endpoint_sha256": (
                hashlib.sha256(endpoint.encode("utf-8")).hexdigest() if endpoint else None
            ),
            "is_local": is_local,
            "transport": (
                "ollama_native"
                if uses_native
                else "openai_compatible"
                if provider_name == "openai_compatible"
                else "none"
            ),
            "generation": generation,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def providers_share_runtime_configuration(
    expected: SummaryProvider, current: SummaryProvider
) -> bool:
    """실행 중 설정 변경 여부를 비밀을 저장·출력하지 않고 비교한다.

    체크포인트 지문에는 API 키를 넣지 않는다. 다만 진행 중인 외부 작업이 키 변경 뒤에도
    예전 provider 객체의 키로 계속 전송하면 안 되므로, 메모리 안에서만 상수시간 비교한다.
    """
    if not getattr(current, "available", False):
        return False
    # digest 조회 불가 시 checkpoint에는 실행별 fail-safe generation을 넣지만, 그 값은
    # factory를 재해석할 때마다 새로 생기므로 active-run 설정 비교에서는 제외한다.
    if provider_checkpoint_fingerprint(
        expected, include_fail_safe_generation=False
    ) != provider_checkpoint_fingerprint(current, include_fail_safe_generation=False):
        return False
    if getattr(expected, "provider_name", "") != "openai_compatible":
        return True
    expected_key = str(getattr(expected, "_api_key", "") or "")
    current_key = str(getattr(current, "_api_key", "") or "")
    return hmac.compare_digest(expected_key, current_key)


class DisabledSummaryProvider:
    """기본 상태 — 요약 모델이 연결되지 않음. 앱 전체는 정상 동작해야 한다."""

    provider_name = "disabled"
    model_name = "disabled"
    available = False
    supports_structured_output = False

    def summarize_group(self, request: GroupRequest) -> GroupSummary:
        raise RuntimeError("요약 공급자가 비활성 상태입니다. 호출 전 available을 확인하세요.")

    def summarize_document(self, request: DocumentRequest) -> dict:
        raise RuntimeError("요약 공급자가 비활성 상태입니다. 호출 전 available을 확인하세요.")


class DeterministicSummaryProvider:
    """테스트 전용 — 실제 의미 요약이 아니라 청크에서 규칙 기반으로 구조화 출력을 만든다.

    같은 입력은 항상 같은 출력을 낸다. 모든 항목은 실제 chunk id만 참조한다(존재하지
    않는 출처를 만들지 않는다). 수치·대상 집단은 파이프라인의 결정론적 추출이 담당하므로
    여기서는 만들지 않는다.
    """

    provider_name = "deterministic"
    model_name = "deterministic-test-v1"
    available = True
    supports_structured_output = True

    def summarize_group(self, request: GroupRequest) -> GroupSummary:
        joined = " ".join(c.text for c in request.chunks).strip()
        summary = joined[:GROUP_SUMMARY_MAX_CHARS]
        return GroupSummary(
            group_id=request.group_id,
            section_title=request.section_title,
            summary_text=summary,
            source_chunk_ids=[c.chunk_id for c in request.chunks],
        )

    def summarize_document(self, request: DocumentRequest) -> dict:
        groups = request.group_summaries
        all_chunk_ids: list[str] = []
        for g in groups:
            for cid in g.source_chunk_ids:
                if cid not in all_chunk_ids:
                    all_chunk_ids.append(cid)

        overview_text = " ".join(g.summary_text for g in groups)[:500].strip()
        result: dict = {
            "overview": {
                "text": overview_text or "문서 요약",
                "sourceChunkIds": all_chunk_ids,
            },
            "sections": [],
            "keyConcepts": [],
            "prerequisites": [],
            "importantNumbers": [],
            "targetPopulations": [],
            "learnerExplanations": [],
            "studyCautions": [],
        }
        if request.include_sections:
            for i, g in enumerate(groups):
                if not g.source_chunk_ids:
                    continue
                result["sections"].append(
                    {
                        "title": g.section_title or f"구역 {i + 1}",
                        "summary": g.summary_text or g.section_title or "",
                        "sourceChunkIds": g.source_chunk_ids,
                    }
                )
                # 핵심 개념: 각 그룹의 섹션 제목을 개념으로 (결정론적, 실제 출처 보존)
                if g.section_title:
                    result["keyConcepts"].append(
                        {
                            "term": g.section_title,
                            "explanation": g.summary_text[:200],
                            "sourceChunkIds": g.source_chunk_ids,
                        }
                    )
        result["learnerExplanations"].append(
            {
                "level": request.learner_level,
                "text": overview_text or "학습자 설명",
                "sourceChunkIds": all_chunk_ids,
            }
        )
        return result


class OpenAICompatibleSummaryProvider:
    """OpenAI 호환 chat/completions 공급자. 실제 네트워크 호출은 CI에서 실행하지 않는다
    (mock transport로만 단위 테스트). API 키·endpoint는 런타임 설정에서 주입받는다.
    """

    provider_name = "openai_compatible"
    supports_structured_output = True

    def __init__(
        self,
        *,
        endpoint: str,
        model_name: str,
        api_key: str,
        is_local: bool,
        http_client=None,
        timeout: float | None = None,
        model_digest: str | None = None,
        provider_identity_digest: str | None = None,
        provider_identity_generation: str | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self.model_name = model_name
        self._api_key = api_key
        self.is_local = is_local
        self._http = http_client  # 테스트에서 mock 주입; None이면 안전 HTTP 경로를 쓴다
        self._timeout = timeout  # None이면 요약 기본 timeout
        self.model_digest = model_digest
        self.provider_identity_digest = provider_identity_digest
        self.provider_identity_generation = provider_identity_generation
        # 실행기가 설치하는 동기 callback. 실제 HTTP 전송 직전에 최신 취소/revision,
        # 외부 동의와 provider 설정을 다시 확인한다. 일반 단위 테스트와 연결 확인에서는 None.
        self._before_request = None
        # 로컬(Ollama 등)은 API 키가 필요 없다 — 외부만 키를 요구한다.
        self.available = bool(
            self._endpoint and self.model_name and (self.is_local or self._api_key)
        )

    def set_before_request_guard(self, guard) -> None:
        """실제 네트워크 요청 직전에 실행할 동기 가드(실행기 전용)."""
        self._before_request = guard

    def _guard_request(self) -> None:
        if self._before_request is not None:
            self._before_request()

    def _request_json(
        self,
        url: str,
        payload: dict,
        *,
        is_local: bool,
        budget: ProviderRequestBudget,
    ) -> dict:
        """동일 HTTP payload만 transient 오류에 재전송한다.

        출력 예산 확대·스키마 재질의까지 공개 provider 메서드 전체를 다시 실행하면 실제
        요청이 4×3으로 증폭한다. 모든 논리 재질의가 공유하는 request budget/deadline에서
        이 단일 전송만 최대 3회 재시도한다.
        """
        from app.services.summary.endpoint import SummaryNetworkError, post_json
        from app.services.summary.settings import (
            SUMMARY_HTTP_MAX_ATTEMPTS,
            SUMMARY_MAX_RESPONSE_BYTES,
            SUMMARY_REQUEST_TIMEOUT_SEC,
        )

        transient = {"timeout", "connect_failed", "rate_limited", "server_error"}
        per_request_timeout = self._timeout or SUMMARY_REQUEST_TIMEOUT_SEC
        for retry_index in range(SUMMARY_HTTP_MAX_ATTEMPTS):
            # 취소/revision/동의/provider 설정은 actual request마다, retry 직전에도 확인한다.
            # 가드가 실패한 호출은 실제 HTTP 요청이 아니므로 request budget을 쓰지 않는다.
            # 반대로 가드가 오래 걸렸다면 그 시간은 node deadline에 포함돼야 하므로,
            # 가드가 끝난 뒤 남은 시간으로 timeout을 계산한다.
            self._guard_request()
            timeout = budget.claim_request(per_request_timeout)
            try:
                if self._http is not None:
                    data = self._http(url, payload, self._api_key)
                else:
                    data = post_json(
                        url,
                        payload,
                        self._api_key,
                        is_local=is_local,
                        timeout=timeout,
                        max_response_bytes=SUMMARY_MAX_RESPONSE_BYTES,
                    )
            except SummaryNetworkError as exc:
                can_retry = (
                    exc.category in transient
                    and retry_index + 1 < SUMMARY_HTTP_MAX_ATTEMPTS
                    and budget.requests_started < budget.request_limit
                )
                if not can_retry:
                    raise
                delay = _retry_delay(exc, retry_index)
                if delay >= budget.remaining_seconds():
                    raise
                logger.info(
                    "summary_provider_request_retry",
                    attempt=retry_index + 1,
                    failure_category=exc.category,
                    failure_reason=exc.reason or "none",
                    retry_delay_ms=int(delay * 1000),
                )
                _sleep_before_request_retry(delay)
                continue

            try:
                parsed = json.loads(data) if isinstance(data, str) else data
            except (json.JSONDecodeError, TypeError) as exc:
                raise SummaryNetworkError("bad_response", "envelope_not_json") from exc
            if not isinstance(parsed, dict):
                raise SummaryNetworkError("bad_response", "envelope_not_json")
            return parsed

        raise AssertionError("HTTP retry loop exhausted without returning or raising")

    def _chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        local_max_tokens: int | None = None,
        budget: ProviderRequestBudget,
    ) -> str:
        from app.services.summary.endpoint import parse_chat_content
        from app.services.summary.settings import (
            LOCAL_MAX_TOKENS,
            LOCAL_REASONING_EFFORT,
        )

        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if self.is_local:
            # 로컬 Qwen3: 비사고·결정론적 + 출력 상한. 외부 provider 계약은 건드리지 않는다.
            payload["temperature"] = 0
            payload["reasoning_effort"] = LOCAL_REASONING_EFFORT
            if max_tokens is None:
                # 호출자가 의도한 출력 예산이 있으면 그걸 쓴다. 기본값(LOCAL_MAX_TOKENS)만
                # 쓰면 reduce처럼 예산이 큰 호출이 로컬 비-Ollama 서버(LM Studio 등)에서만
                # 2048로 잘려 finish_length로 문서 전체가 실패한다 — native 경로에만
                # 적용된 이번 수정이 같은 로컬 사용자에게 닿지 않는 공백이었다.
                payload["max_tokens"] = local_max_tokens or LOCAL_MAX_TOKENS
        url = f"{self._endpoint}/chat/completions"
        data = self._request_json(url, payload, is_local=self.is_local, budget=budget)
        return parse_chat_content(data)

    def _native_url(self) -> str:
        from app.services.summary.endpoint import ollama_native_chat_url

        return ollama_native_chat_url(self._endpoint)

    def _chat_native(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        schema: dict | None,
        budget: ProviderRequestBudget,
    ) -> str:
        """로컬(Ollama) 전용 경로 — 컨텍스트를 직접 지정하고 응답 구조를 강제한다.

        OpenAI 호환 경로는 num_ctx를 받지 않아 기기별 기본 컨텍스트에 성패가 좌우됐다.
        여기서는 num_ctx를 명시하고, JSON schema로 계약 위반 자체를 줄인다.
        """
        from app.services.model_output import strip_thinking
        from app.services.summary.endpoint import SummaryNetworkError
        from app.services.summary.settings import (
            LOCAL_KEEP_ALIVE,
            LOCAL_NUM_CTX,
        )

        payload: dict = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "think": False,  # 사고 흔적이 출력 예산을 잡아먹지 않게 명시적으로 끈다
            "keep_alive": LOCAL_KEEP_ALIVE,
            "options": {
                "temperature": 0,
                "num_ctx": LOCAL_NUM_CTX,
                "num_predict": max_tokens,
            },
        }
        payload["format"] = schema if schema is not None else "json"

        data = self._request_json(self._native_url(), payload, is_local=True, budget=budget)

        try:
            content = data["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SummaryNetworkError("bad_response", "envelope_shape") from exc
        # native는 done_reason으로 중단 사유를 준다(OpenAI의 finish_reason과 같은 역할).
        done_reason = data.get("done_reason")
        if done_reason not in (None, "stop"):
            safe = done_reason if done_reason in _KNOWN_FINISH_REASONS else "other"
            raise SummaryNetworkError("bad_response", f"finish_{safe}")
        # 프롬프트가 창을 가득 채웠다면 Ollama가 앞부분을 잘라낸 것이다. 모델이 자백하지
        # 않으면(대개 잘린 뒷부분만 보고도 그럴듯한 요약을 만들어낸다) 잘린 입력의 부분
        # 요약이 그룹 전체의 출처를 주장하며 성공으로 저장된다. 문구 매칭에만 의존하지
        # 않고, 서버가 num_ctx를 지정했으므로 직접 셀 수 있는 이 값으로 확정한다.
        prompt_tokens = data.get("prompt_eval_count")
        if isinstance(prompt_tokens, int) and prompt_tokens >= LOCAL_NUM_CTX:
            raise SummaryNetworkError("context_overflow", "native_prompt_truncated")
        if not isinstance(content, str):
            raise SummaryNetworkError("bad_response", "content_not_text")
        return strip_thinking(content)

    @property
    def uses_ollama_native(self) -> bool:
        """Ollama일 때만 native 경로를 쓴다.

        `is_local`은 "loopback이다"라는 뜻이지 "Ollama다"가 아니다. LM Studio·llama.cpp
        server·vLLM 같은 로컬 OpenAI 호환 서버는 /api/chat을 제공하지 않으므로, 앱이
        직접 붙인 Ollama endpoint일 때만 native로 보낸다.

        비교는 문자열이 아니라 **정규화된 (loopback, 포트, 경로)**로 한다.
        `validate_endpoint`는 호스트명을 IP로 바꾸지 않으므로, 같은 Ollama를 가리키는
        `http://localhost:11434/v1`이 저장돼 있으면 문자열 일치로는 걸러지지 않아
        num_ctx 지정·JSON schema 강제가 통째로 적용되지 않았다.
        """
        from app.services.summary.endpoint import is_ollama_native_endpoint

        return is_ollama_native_endpoint(self._endpoint, self.is_local)

    def _chat_for(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        schema: dict | None,
        external_max_tokens: int | None,
        budget: ProviderRequestBudget,
    ) -> str:
        """Ollama는 native, 그 외에는 OpenAI 호환 경로.

        `external_max_tokens`는 OpenAI 호환 경로로 나가는 값이다. reduce처럼 원래
        max_tokens를 보내지 않던 호출은 None을 넘겨 기존 계약을 유지한다 — 요청량이
        모델 상한을 넘으면 400을 받아 모든 문서가 실패하기 때문이다.
        """
        if self.uses_ollama_native:
            return self._chat_native(
                system, user, max_tokens=max_tokens, schema=schema, budget=budget
            )
        return self._chat(
            system,
            user,
            max_tokens=external_max_tokens,
            local_max_tokens=max_tokens,
            budget=budget,
        )

    @staticmethod
    def _parse_json_object(raw: str, *, stage: str) -> dict:
        from app.services.summary.endpoint import SummaryNetworkError

        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SummaryNetworkError("bad_response", f"{stage}_content_not_json") from exc
        if not isinstance(parsed, dict):
            raise SummaryNetworkError("bad_response", f"{stage}_content_not_object")
        return parsed

    def summarize_group(self, request: GroupRequest) -> GroupSummary:
        budget = _ACTIVE_REQUEST_BUDGET.get() or new_provider_request_budget(
            total_deadline_seconds=self._timeout
        )
        chunk_block = "\n\n".join(
            f"[입력 {index}] {c.text}" for index, c in enumerate(request.chunks, start=1)
        )
        system = (
            "너는 의료 학습자료를 짧게 압축하는 보조 도구다. 입력 내용만 사용하고 "
            "새로운 임상 판단·진단·처방을 만들지 마라. 사고 과정이나 마크다운 없이 "
            "JSON 객체만 답하라."
        )
        user = (
            f"학습자 수준: {request.learner_level}. 다음 입력 전체를 한국어 3문장 이내, "
            f'400자 이하로 요약하라. 정확히 {{"summary":"..."}} 한 필드만 출력하고 '
            f"출처 ID·페이지·좌표는 출력하지 마라.\n\n{chunk_block}"
        )
        from app.services.summary.endpoint import SummaryNetworkError

        # 오류 형태를 막는 지시를 프롬프트에도 넣는다. 스키마로만 표현하면 Ollama
        # native 경로에만 적용되고, OpenAI 호환 경로(_chat)는 schema를 보내지 않아
        # 재질의가 첫 호출과 바이트 단위로 같은 요청이 된다 — 로컬 서버는 temperature
        # 0이라 같은 error가 그대로 돌아오고, 주석이 약속한 구제가 닿지 않는다.
        no_error_clause = (
            "\n\n앞선 시도에서 error 객체를 받았다. 이번에는 error 형태로 답하지 말고 "
            '반드시 {"summary":"..."} 형태로만 답하라.'
        )

        def call(max_tokens: int, *, allow_error: bool = True) -> str:
            return self._chat_for(
                system,
                user if allow_error else user + no_error_clause,
                max_tokens=max_tokens,
                schema=_map_schema(allow_error=allow_error),
                external_max_tokens=max_tokens,
                budget=budget,
            )

        def call_with_budget(*, allow_error: bool = True) -> str:
            """출력 상한에 걸리면 예산을 한 번 늘려 다시 부른다.

            temperature 0이라 같은 예산으로 재시도하면 같은 지점에서 잘린다. 그래도
            넘치면 별도 reason으로 올려 executor가 입력을 나눠 적응하게 한다 —
            날것의 `finish_length`를 올리면 `_is_input_too_large()`가 그것을 명시적으로
            제외하므로 적응 분할이 발동하지 못하고 그 그룹은 그냥 실패한다.
            """
            try:
                return call(SUMMARY_MAP_MAX_TOKENS, allow_error=allow_error)
            except SummaryNetworkError as exc:
                if exc.reason != "finish_length":
                    raise
                try:
                    return call(SUMMARY_MAP_RETRY_MAX_TOKENS, allow_error=allow_error)
                except SummaryNetworkError as retry_exc:
                    if retry_exc.reason == "finish_length":
                        raise SummaryNetworkError(
                            "bad_response", "map_finish_length"
                        ) from retry_exc
                    raise

        raw = call_with_budget()
        parsed = self._parse_json_object(raw, stage="map")
        summary = parsed.get("summary")
        if not isinstance(summary, str):
            # 컨텍스트 창을 넘으면 Ollama가 프롬프트를 잘라 모델이 error 객체를 돌려준다.
            # "응답 형식 오류"로 뭉뚱그리면 사용자가 손쓸 방법을 알 수 없으므로 구분한다.
            if _looks_like_context_overflow(parsed):
                raise SummaryNetworkError("context_overflow", "map_context_overflow")
            # 초과 징후가 없는 error다 — 스키마가 열어 준 탈출구를 모델이 고른 것이므로
            # 오류 형태를 뺀 문법으로 한 번 더 물어본다. 이게 없으면 그 청크는 영구히
            # 요약되지 않고 문서 전체가 실패한다.
            #
            # 이 재질의도 예산 확대를 거친다. error를 못 쓰게 됐으니 모델은 이제 긴
            # 요약을 쓰기 시작하고, 첫 호출보다 오히려 상한에 걸리기 쉽다. 예산 확대
            # 밖에 두면 그 자리에서 날것의 finish_length가 올라가고, _is_input_too_large()가
            # 그것을 제외하므로 executor의 적응 분할도 발동하지 않아 복구 경로가 사라진다.
            parsed = self._parse_json_object(call_with_budget(allow_error=False), stage="map")
            summary = parsed.get("summary")
        if not isinstance(summary, str):
            raise SummaryNetworkError("bad_response", "map_summary_missing")
        if not summary.strip():
            raise SummaryNetworkError("bad_response", "map_summary_empty")
        if len(summary.strip()) > GROUP_SUMMARY_MAX_CHARS:
            # 실행기가 입력을 나눠 다시 시도할 수 있게 예외로 올린다. 다만 출력을 함께
            # 실어 보낸다 — 나눠도 계속 넘기는 모델(스키마 강제가 없는 경로에서 흔하다)
            # 이면 실행기가 이 값을 잘라 쓰는 것 말고 할 수 있는 게 없고, 그러지 못하면
            # 그룹 하나의 계약 위반으로 문서 전체 요약이 사라진다.
            raise SummaryNetworkError(
                "bad_response", "map_summary_too_long", oversized_text=summary.strip()
            )
        source_ids = list(dict.fromkeys(c.chunk_id for c in request.chunks))
        return GroupSummary(
            group_id=request.group_id,
            section_title=request.section_title,
            summary_text=summary.strip(),
            source_chunk_ids=source_ids,
        )

    def summarize_document(self, request: DocumentRequest) -> dict:
        budget = _ACTIVE_REQUEST_BUDGET.get() or new_provider_request_budget(
            total_deadline_seconds=self._timeout
        )
        group_block = "\n\n".join(
            f"[group {g.group_id}] {g.summary_text}" for g in request.group_summaries
        )
        system = (
            "너는 의료 학습자료를 구조화 요약하는 보조 도구다. 주어진 그룹 요약과 그 "
            "group id만 사용하라. 존재하지 않는 group id나 새로운 임상 판단을 만들지 "
            "마라. page/bbox/chunk id는 출력하지 마라. JSON으로만 답하라."
        )
        # 형태를 예시로 못박는다. "각 항목에 sourceGroupIds를 포함하라"만으로는 overview를
        # 문자열로 돌려줘 서버가 조용히 버리는 일이 실측에서 반복됐다(출처 없는 항목은
        # 저장하지 않는 계약이라 개요가 통째로 사라진다).
        valid_ids = ", ".join(g.group_id for g in request.group_summaries)
        # 예시도 요청된 항목만 보여준다. 끈 항목을 예시로 남기면 모델이 그대로 채워
        # 보내고(스키마가 강제되지 않는 외부·비-Ollama 경로에서는 예시가 유일한 계약이다)
        # 사용자가 명시적으로 끈 구역 요약·선수지식이 그대로 저장된다.
        shape = ['"overview":{"text":"...","sourceGroupIds":["g0"]}']
        if request.include_sections:
            shape.append('"sections":[{"title":"...","summary":"...","sourceGroupIds":["g0"]}]')
        shape.append('"keyConcepts":[{"term":"...","explanation":"...","sourceGroupIds":["g0"]}]')
        if request.include_prerequisites:
            shape.append(
                '"prerequisites":[{"concept":"...","whyNeeded":"...",'
                '"sourceType":"document","sourceGroupIds":["g0"]}]'
            )
        shape.append(
            '"learnerExplanations":[{"level":"'
            + request.learner_level
            + '","text":"...","sourceGroupIds":["g0"]}]'
        )
        user = (
            f"학습자 수준: {request.learner_level}, 언어: {request.language}. "
            "다음 그룹 요약으로 구조화 요약을 만들어라. 아래 형태를 정확히 지켜라. "
            "모든 객체는 sourceGroupIds 배열을 가져야 하며 그 값은 "
            f"[{valid_ids}] 중에서만 고른다.\n"
            "{" + ",".join(shape) + "}"
            f"\n\n{group_block}"
        )
        from app.services.summary.endpoint import SummaryNetworkError

        try:
            raw = self._chat_for(
                system,
                user,
                max_tokens=SUMMARY_REDUCE_MAX_TOKENS,
                schema=_document_schema(
                    include_sections=request.include_sections,
                    include_prerequisites=request.include_prerequisites,
                ),
                # 외부 공급자에는 reduce max_tokens를 보내지 않는다(변경 전 계약 유지).
                external_max_tokens=None,
                budget=budget,
            )
        except SummaryNetworkError as exc:
            if exc.reason != "finish_length":
                raise
            # map은 여기서 더 큰 예산으로 한 번 더 부르지만 reduce는 그럴 수 없다 —
            # 프롬프트 약 2,000토큰 + 출력 4,096으로 이미 num_ctx(8192) 상한 근처라
            # 예산을 키우면 이번엔 컨텍스트가 넘친다. 남은 수단은 입력을 줄이는 것이므로,
            # 실행기가 그렇게 읽을 수 있는 reason으로 승격한다. 승격하지 않으면 날것의
            # finish_length가 '줄여도 소용없는 실패'로 분류돼 접기 재시도가 한 번도
            # 발동하지 않고 문서 전체 요약이 첫 절단에서 영구히 죽는다.
            raise SummaryNetworkError("bad_response", "reduce_finish_length") from exc
        parsed = self._parse_json_object(raw, stage="reduce")
        # reduce에도 초과 감지가 필요하다. 없으면 잘린 응답이 예외 없이 통과해
        # artifact가 하나도 없는 '빈 요약'이 SUCCEEDED로 저장된다.
        #
        # 판정 기준은 "overview 키가 있는가"가 아니라 "저장할 내용이 하나라도 있는가"다.
        # 키만 보면 양쪽으로 어긋난다 — overview를 문자열로 돌려준 응답(실측에서 반복된
        # 형태)은 통과시켜 빈 요약이 SUCCEEDED가 되고, 반대로 sections·keyConcepts가
        # 멀쩡한데 overview만 빠진 응답은 문서 전체를 실패시킨다(temperature 0이라
        # 재시도해도 같은 응답이 나와 그 문서는 영구히 요약을 얻지 못한다).
        if not _has_usable_content(parsed):
            if _looks_like_context_overflow(parsed):
                raise SummaryNetworkError("context_overflow", "reduce_context_overflow")
            raise SummaryNetworkError("bad_response", "reduce_empty_summary")
        return self._resolve_group_sources(parsed, request)

    @staticmethod
    def _resolve_group_sources(structured: dict, request: DocumentRequest) -> dict:
        """모델 group token을 서버 소유 원본 chunk id로 바꾸고 모델 chunk id는 버린다.

        **저장 대상 하위 트리만** 검증한다. 모델이 덧붙인 항목이나 사용자가 끈 항목까지
        훑으면, 어차피 버려질 곳의 sourceGroupIds 하나가 형식을 어겼다고 문서 전체가
        영구 실패한다(temperature 0이라 재시도해도 같은 응답이 나온다).
        """
        from app.services.summary.endpoint import SummaryNetworkError

        keep = {"overview", "keyConcepts", "learnerExplanations"}
        if request.include_sections:
            keep.add("sections")
        if request.include_prerequisites:
            keep.add("prerequisites")
        structured = {k: v for k, v in structured.items() if k in keep}

        group_sources = {
            group.group_id: list(dict.fromkeys(group.source_chunk_ids))
            for group in request.group_summaries
        }

        def resolve(value):
            if isinstance(value, list):
                return [resolve(item) for item in value]
            if not isinstance(value, dict):
                return value
            out = {key: resolve(item) for key, item in value.items() if key != "sourceChunkIds"}
            if "sourceGroupIds" in value:
                tokens = value["sourceGroupIds"]
                if not isinstance(tokens, list) or not tokens:
                    raise SummaryNetworkError("bad_response", "reduce_group_ids_shape")
                if not all(isinstance(token, str) and token in group_sources for token in tokens):
                    raise SummaryNetworkError("bad_response", "reduce_group_ids_unknown")
                resolved: list[str] = []
                for token in dict.fromkeys(tokens):
                    for chunk_id in group_sources[token]:
                        if chunk_id not in resolved:
                            resolved.append(chunk_id)
                out.pop("sourceGroupIds", None)
                out["sourceChunkIds"] = resolved
            return out

        return resolve(structured)


def build_summary_provider(config, *, timeout: float | None = None) -> SummaryProvider:
    """설정으로부터 공급자를 만든다. config가 None/비활성이면 Disabled.

    config는 provider_type/endpoint/model_name/api_key/is_local 속성을 가진 객체.
    timeout을 주면(연결 확인 등) 그 값을 요청 전체 timeout으로 쓴다.
    """
    if config is None or not getattr(config, "enabled", False):
        return DisabledSummaryProvider()
    ptype = getattr(config, "provider_type", "disabled")
    if ptype == "deterministic":
        return DeterministicSummaryProvider()
    if ptype == "openai_compatible":
        return OpenAICompatibleSummaryProvider(
            endpoint=config.endpoint or "",
            model_name=config.model_name or "",
            api_key=getattr(config, "api_key", "") or "",
            is_local=bool(getattr(config, "is_local", False)),
            timeout=timeout,
            model_digest=getattr(config, "model_digest", None),
            provider_identity_digest=getattr(config, "provider_identity_digest", None),
            provider_identity_generation=getattr(config, "provider_identity_generation", None),
        )
    return DisabledSummaryProvider()
