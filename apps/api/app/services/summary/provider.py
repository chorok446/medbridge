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
    STUDY_CAUTION_NOTICE,
)


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
            "studyCautions": [
                {"text": STUDY_CAUTION_NOTICE, "sourceChunkIds": all_chunk_ids[:1]}
            ],
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
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self.model_name = model_name
        self._api_key = api_key
        self.is_local = is_local
        self._http = http_client  # 테스트에서 mock 주입; None이면 호출 시점에 생성
        self.available = bool(self._endpoint and self.model_name and self._api_key)

    def _chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        # 테스트에서 http_client(콜러블)를 주입하면 그것을 쓴다 — 실제 네트워크 없이 검증.
        if self._http is not None:
            return self._http(f"{self._endpoint}/chat/completions", payload, self._api_key)
        # 런타임은 stdlib urllib만 사용한다(httpx는 sidecar 번들에 없음).
        import json as _json
        import urllib.request

        req = urllib.request.Request(
            f"{self._endpoint}/chat/completions",
            data=_json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60.0) as resp:  # noqa: S310 (신뢰된 사용자 설정 endpoint)
            data = _json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    def summarize_group(self, request: GroupRequest) -> GroupSummary:
        chunk_block = "\n\n".join(
            f"[{c.chunk_id}] {c.text}" for c in request.chunks
        )
        system = (
            "너는 의료 학습자료를 요약하는 보조 도구다. 주어진 청크 내용만 사용하고 "
            "새로운 임상 판단·진단·처방을 만들지 마라. JSON으로만 답하라."
        )
        user = (
            f"학습자 수준: {request.learner_level}. 다음 청크들을 한국어로 요약하라. "
            f'형식: {{"summary": "...", "sourceChunkIds": ["청크id"]}}\n\n{chunk_block}'
        )
        raw = self._chat(system, user)
        parsed = json.loads(raw)
        ids = [str(i) for i in parsed.get("sourceChunkIds", [])]
        return GroupSummary(
            group_id=request.group_id,
            section_title=request.section_title,
            summary_text=str(parsed.get("summary", ""))[:GROUP_SUMMARY_MAX_CHARS],
            source_chunk_ids=ids or [c.chunk_id for c in request.chunks],
        )

    def summarize_document(self, request: DocumentRequest) -> dict:
        group_block = "\n\n".join(
            f"[group {g.group_id} · chunks {','.join(g.source_chunk_ids)}] {g.summary_text}"
            for g in request.group_summaries
        )
        system = (
            "너는 의료 학습자료를 구조화 요약하는 보조 도구다. 주어진 그룹 요약과 그 "
            "sourceChunkIds만 사용하라. 존재하지 않는 청크 id나 새로운 임상 판단을 "
            "만들지 마라. page/bbox는 출력하지 마라. JSON으로만 답하라."
        )
        user = (
            f"학습자 수준: {request.learner_level}, 언어: {request.language}. "
            "다음 그룹 요약으로 구조화 요약(overview, sections, keyConcepts, "
            "prerequisites, learnerExplanations, studyCautions)을 만들어라. 각 항목에 "
            f"sourceChunkIds를 포함하라.\n\n{group_block}"
        )
        raw = self._chat(system, user)
        return json.loads(raw)


def build_summary_provider(config) -> SummaryProvider:
    """설정으로부터 공급자를 만든다. config가 None/비활성이면 Disabled.

    config는 provider_type/endpoint/model_name/api_key/is_local 속성을 가진 객체.
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
        )
    return DisabledSummaryProvider()
