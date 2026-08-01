"""요약 공급자 인터페이스 — EmbeddingProvider와 완전히 분리된다.

공급자는 chunk id + 텍스트 + section_title만 본다. page/bbox 출처는 절대 다루지 않는다
(서버가 chunk의 저장된 source_refs에서 재구성한다). 실제 API 키·endpoint·모델명을
코드에 하드코딩하지 않는다.
"""

import json
from dataclasses import dataclass, field
from typing import Protocol

from app.services.summary.settings import (
    GROUP_SUMMARY_MAX_CHARS,
    SUMMARY_MAP_MAX_TOKENS,
    SUMMARY_REDUCE_MAX_TOKENS,
)

# finish_reason 값을 로그에 남길 때 모델이 준 임의 문자열을 그대로 쓰지 않는다.
_KNOWN_FINISH_REASONS = frozenset({"length", "content_filter", "tool_calls", "function_call"})

# 프롬프트가 컨텍스트 창을 넘어 잘렸을 때 모델이 내놓는 응답의 표지.
_OVERFLOW_HINTS = ("too long", "too large", "context", "excessive repetition")


def _looks_like_context_overflow(parsed: dict) -> bool:
    """계약 필드 대신 error 객체가 온 경우 — 입력 초과인지 판별한다.

    Ollama는 프롬프트를 조용히 자르므로 HTTP는 200이고 usage도 잘린 값이 온다. 유일한
    단서가 모델이 낸 error 문구뿐이라 여기서만 본문을 들여다본다(로그에는 남기지 않는다).
    """
    if "summary" in parsed:
        return False
    error = parsed.get("error")
    if not isinstance(error, str):
        return False
    lowered = error.lower()
    return any(hint in lowered for hint in _OVERFLOW_HINTS)


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
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self.model_name = model_name
        self._api_key = api_key
        self.is_local = is_local
        self._http = http_client  # 테스트에서 mock 주입; None이면 안전 HTTP 경로를 쓴다
        self._timeout = timeout  # None이면 요약 기본 timeout
        # 로컬(Ollama 등)은 API 키가 필요 없다 — 외부만 키를 요구한다.
        self.available = bool(
            self._endpoint and self.model_name and (self.is_local or self._api_key)
        )

    def _chat(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        from app.services.model_output import strip_thinking
        from app.services.summary.endpoint import SummaryNetworkError, post_json
        from app.services.summary.settings import (
            LOCAL_MAX_TOKENS,
            LOCAL_REASONING_EFFORT,
            SUMMARY_MAX_RESPONSE_BYTES,
            SUMMARY_REQUEST_TIMEOUT_SEC,
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
                payload["max_tokens"] = LOCAL_MAX_TOKENS
        url = f"{self._endpoint}/chat/completions"
        # 테스트에서 http_client(콜러블)를 주입하면 그것을 쓴다 — 실제 네트워크 없이 검증.
        try:
            if self._http is not None:
                raw = self._http(url, payload, self._api_key)
                data = json.loads(raw) if isinstance(raw, str) else raw
            else:
                # 런타임은 검증·redirect 차단·크기 제한이 적용된 안전 HTTP 경로만 쓴다.
                data = post_json(
                    url,
                    payload,
                    self._api_key,
                    is_local=self.is_local,
                    timeout=self._timeout or SUMMARY_REQUEST_TIMEOUT_SEC,
                    max_response_bytes=SUMMARY_MAX_RESPONSE_BYTES,
                )
        except (json.JSONDecodeError, TypeError) as exc:
            raise SummaryNetworkError("bad_response", "envelope_not_json") from exc
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SummaryNetworkError("bad_response", "envelope_shape") from exc
        # length/content_filter 등은 완결된 JSON 계약이 아니므로 파싱 전에 명시적으로 거부한다.
        finish_reason = choice.get("finish_reason")
        if finish_reason not in (None, "stop"):
            # 토큰 상한 절단(length)인지 다른 중단인지 구분해 둔다 — 대응이 다르다.
            safe = finish_reason if finish_reason in _KNOWN_FINISH_REASONS else "other"
            raise SummaryNetworkError("bad_response", f"finish_{safe}")
        if not isinstance(content, str):
            raise SummaryNetworkError("bad_response", "content_not_text")
        # thinking 흔적은 UI·저장·로그에 남기지 않는다(JSON 파싱 전에 제거).
        return strip_thinking(content)

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
            f"400자 이하로 요약하라. 정확히 {{\"summary\":\"...\"}} 한 필드만 출력하고 "
            f"출처 ID·페이지·좌표는 출력하지 마라.\n\n{chunk_block}"
        )
        from app.services.summary.endpoint import SummaryNetworkError

        raw = self._chat(system, user, max_tokens=SUMMARY_MAP_MAX_TOKENS)
        parsed = self._parse_json_object(raw, stage="map")
        summary = parsed.get("summary")
        if not isinstance(summary, str):
            # 컨텍스트 창을 넘으면 Ollama가 프롬프트를 잘라 모델이 error 객체를 돌려준다.
            # "응답 형식 오류"로 뭉뚱그리면 사용자가 손쓸 방법을 알 수 없으므로 구분한다.
            if _looks_like_context_overflow(parsed):
                raise SummaryNetworkError("context_overflow", "map_context_overflow")
            raise SummaryNetworkError("bad_response", "map_summary_missing")
        if not summary.strip():
            raise SummaryNetworkError("bad_response", "map_summary_empty")
        if len(summary.strip()) > GROUP_SUMMARY_MAX_CHARS:
            raise SummaryNetworkError("bad_response", "map_summary_too_long")
        source_ids = list(dict.fromkeys(c.chunk_id for c in request.chunks))
        return GroupSummary(
            group_id=request.group_id,
            section_title=request.section_title,
            summary_text=summary.strip(),
            source_chunk_ids=source_ids,
        )

    def summarize_document(self, request: DocumentRequest) -> dict:
        group_block = "\n\n".join(
            f"[group {g.group_id}] {g.summary_text}"
            for g in request.group_summaries
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
        user = (
            f"학습자 수준: {request.learner_level}, 언어: {request.language}. "
            "다음 그룹 요약으로 구조화 요약을 만들어라. 아래 형태를 정확히 지켜라. "
            "모든 객체는 sourceGroupIds 배열을 가져야 하며 그 값은 "
            f"[{valid_ids}] 중에서만 고른다.\n"
            '{"overview":{"text":"...","sourceGroupIds":["g0"]},'
            '"sections":[{"title":"...","summary":"...","sourceGroupIds":["g0"]}],'
            '"keyConcepts":[{"term":"...","explanation":"...","sourceGroupIds":["g0"]}],'
            '"prerequisites":[{"concept":"...","whyNeeded":"...","sourceType":"document",'
            '"sourceGroupIds":["g0"]}],'
            '"learnerExplanations":[{"level":"'
            f'{request.learner_level}","text":"...","sourceGroupIds":["g0"]}}]}}'
            f"\n\n{group_block}"
        )
        raw = self._chat(system, user, max_tokens=SUMMARY_REDUCE_MAX_TOKENS)
        parsed = self._parse_json_object(raw, stage="reduce")
        return self._resolve_group_sources(parsed, request)

    @staticmethod
    def _resolve_group_sources(structured: dict, request: DocumentRequest) -> dict:
        """모델 group token을 서버 소유 원본 chunk id로 바꾸고 모델 chunk id는 버린다."""
        from app.services.summary.endpoint import SummaryNetworkError

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
                if not all(
                    isinstance(token, str) and token in group_sources for token in tokens
                ):
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
        )
    return DisabledSummaryProvider()
