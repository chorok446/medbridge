"""Q&A 공급자 — 요약과 별개의 Protocol이지만 네트워크 안전 경로와 모델 설정·키를
재사용한다(별도 HTTP 클라이언트·키 저장소를 만들지 않는다).

공급자는 chunkId·sectionTitle·text·pageStart/pageEnd만 본다. bbox·저장 경로·내부 DB
구조는 전달하지 않는다.
"""

import json
from dataclasses import dataclass, field
from typing import Protocol

from app.services.qa.settings import (
    ANSWER_MAX_CHARS,
    MAX_CLAIMS,
    MAX_FOLLOWUPS,
)


@dataclass
class QaContextChunk:
    """모델에 전달하는 청크 — bbox·좌표는 포함하지 않는다."""

    chunk_id: str
    section_title: str | None
    text: str
    page_start: int
    page_end: int


@dataclass
class QaHistoryTurn:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class QaRequest:
    question: str
    chunks: list[QaContextChunk]
    history: list[QaHistoryTurn] = field(default_factory=list)
    language: str = "ko"


class QaProvider(Protocol):
    provider_name: str
    model_name: str
    available: bool
    is_local: bool

    def answer(self, request: QaRequest) -> dict: ...


_SYSTEM_PROMPT = (
    "너는 업로드된 의료 학습자료(PDF)에 대한 질문에 답하는 보조 도구다. 규칙:\n"
    "- 제공된 문서 청크의 내용만 사용한다. 문서에 없는 정보는 answerStatus를 "
    "not_found로 하고 '이 자료에서는 확인할 수 없습니다'라고 답한다.\n"
    "- 진단·처방·용량 결정·응급도 판정·실제 환자 의사결정을 하지 않는다.\n"
    "- 상충하는 내용이 있으면 한쪽을 임의로 고르지 말고 conflicting_evidence로 표시한다.\n"
    "- 모든 사실 주장(claim)에는 근거가 된 청크의 chunkId를 sourceChunkIds로 붙인다. "
    "근거 없는 사실 주장을 만들지 않는다.\n"
    "- page나 bbox를 직접 출력하지 않는다.\n"
    "- JSON 외의 텍스트를 출력하지 않는다.\n"
    'JSON 형식: {"answer": "...", "answerStatus": '
    '"answered|not_found|insufficient_evidence|conflicting_evidence", '
    '"claims": [{"text": "...", "sourceChunkIds": ["..."]}], '
    '"followUpSuggestions": ["..."]}'
)


class DisabledQaProvider:
    provider_name = "disabled"
    model_name = "disabled"
    available = False
    is_local = True

    def answer(self, request: QaRequest) -> dict:
        raise RuntimeError("Q&A 공급자가 비활성 상태입니다. 호출 전 available을 확인하세요.")


class DeterministicQaProvider:
    """테스트 전용 — 검색된 청크에서 규칙 기반으로 답변·claim을 만든다(실제 의미 이해 아님).

    문서에 근거가 있으면 첫 청크 텍스트 일부를 answer로, 각 청크를 claim으로 낸다.
    존재하지 않는 chunkId를 만들지 않는다.
    """

    provider_name = "deterministic"
    model_name = "deterministic-qa-v1"
    available = True
    is_local = True

    def answer(self, request: QaRequest) -> dict:
        if not request.chunks:
            return {
                "answer": "이 자료에서는 확인할 수 없습니다.",
                "answerStatus": "not_found",
                "claims": [],
                "followUpSuggestions": [],
            }
        pairs: list[tuple[str, str]] = [
            (c.text[:200].strip() or (c.section_title or ""), c.chunk_id)
            for c in request.chunks[:3]
            if c.text.strip()
        ]
        claims = [{"text": t, "sourceChunkIds": [cid]} for t, cid in pairs]
        answer = " ".join(t for t, _ in pairs)[:ANSWER_MAX_CHARS]
        return {
            "answer": answer or "이 자료에서는 확인할 수 없습니다.",
            "answerStatus": "answered" if claims else "not_found",
            "claims": claims,
            "followUpSuggestions": [],
        }


class OpenAICompatibleQaProvider:
    """OpenAI 호환 chat/completions. 요약과 동일한 안전 HTTP 경로(post_json)를 재사용한다.

    API 키·endpoint·모델명은 요약 설정에서 주입받는다(별도 저장소 없음).
    """

    provider_name = "openai_compatible"

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
        self._http = http_client
        self._timeout = timeout
        # 로컬(Ollama 등) 공급자는 API 키가 필요 없다 — 외부 공급자만 키를 요구한다.
        self.available = bool(
            self._endpoint and self.model_name and (self.is_local or self._api_key)
        )

    def answer(self, request: QaRequest) -> dict:
        from app.services.summary.endpoint import SummaryNetworkError, post_json
        from app.services.summary.settings import (
            LOCAL_MAX_TOKENS,
            LOCAL_REASONING_EFFORT,
            SUMMARY_MAX_RESPONSE_BYTES,
            SUMMARY_REQUEST_TIMEOUT_SEC,
        )

        user = _build_user_prompt(request)
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        if self.is_local:
            # 로컬 Qwen3: 비사고·결정론적 + 출력 상한. 외부 provider 계약은 유지.
            payload["temperature"] = 0
            payload["reasoning_effort"] = LOCAL_REASONING_EFFORT
            payload["max_tokens"] = LOCAL_MAX_TOKENS
        url = f"{self._endpoint}/chat/completions"
        if self._http is not None:
            raw = self._http(url, payload, self._api_key)
            data = json.loads(raw) if isinstance(raw, str) else raw
        else:
            data = post_json(
                url,
                payload,
                self._api_key,
                is_local=self.is_local,
                timeout=self._timeout or SUMMARY_REQUEST_TIMEOUT_SEC,
                max_response_bytes=SUMMARY_MAX_RESPONSE_BYTES,
            )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SummaryNetworkError("bad_response") from exc
        if isinstance(content, str):
            from app.services.model_output import strip_thinking

            content = strip_thinking(content)  # thinking 흔적 제거 후 JSON 파싱
        try:
            return json.loads(content) if isinstance(content, str) else content
        except (ValueError, TypeError) as exc:
            raise SummaryNetworkError("bad_response") from exc


def _build_user_prompt(request: QaRequest) -> str:
    parts: list[str] = []
    if request.history:
        hist = "\n".join(f"[{t.role}] {t.content}" for t in request.history)
        parts.append(f"이전 대화(질문 해석용 참고, 근거로 쓰지 마라):\n{hist}")
    chunk_block = "\n\n".join(
        f"[chunkId {c.chunk_id}] (제목: {c.section_title or '없음'}, "
        f"{c.page_start}-{c.page_end}쪽)\n{c.text}"
        for c in request.chunks
    )
    parts.append(f"문서 청크:\n{chunk_block}")
    parts.append(f"질문: {request.question}")
    parts.append(
        f"위 청크만 근거로 JSON으로 답하라. claims는 최대 {MAX_CLAIMS}개, "
        f"followUpSuggestions는 최대 {MAX_FOLLOWUPS}개, 문서 범위 질문만."
    )
    return "\n\n".join(parts)


def build_qa_provider(config, *, timeout: float | None = None) -> QaProvider:
    """요약 모델 설정(ResolvedProviderConfig 형태)으로 Q&A 공급자를 만든다."""
    if config is None or not getattr(config, "enabled", False):
        return DisabledQaProvider()
    ptype = getattr(config, "provider_type", "disabled")
    if ptype == "deterministic":
        return DeterministicQaProvider()
    if ptype == "openai_compatible":
        return OpenAICompatibleQaProvider(
            endpoint=config.endpoint or "",
            model_name=config.model_name or "",
            api_key=getattr(config, "api_key", "") or "",
            is_local=bool(getattr(config, "is_local", False)),
            timeout=timeout,
        )
    return DisabledQaProvider()
