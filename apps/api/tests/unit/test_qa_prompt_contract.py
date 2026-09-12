"""스트리밍·비스트리밍 모두 출처 필수 및 관련 설명 보류 계약을 쓴다."""

from app.services.qa import provider, streaming
from app.services.qa.prompt_contract import CLAIM_SOURCE_AND_ABSTENTION_RULE
from app.services.qa.provider import QaContextChunk, QaRequest


def test_both_qa_paths_require_sources_and_abstention():
    for prompt in (provider._SYSTEM_PROMPT, streaming._SYSTEM_PROMPT):
        assert CLAIM_SOURCE_AND_ABSTENTION_RULE in prompt
        assert "sourceChunkIds를 대체하지" in prompt
        assert "claim을 하나도 만들지 않고" in prompt
        assert "claim의 text에 붙이지 않는다" in prompt
        assert "같은 언어면 번역용 원문 괄호를" in prompt
        assert "원문 인용을 붙여도" in prompt
        assert "없는 정의·기능·기전은 추가하지 않는다" in prompt
        assert "실제로 사용한 모든 조각의 chunkId" in prompt
        assert "중간 조각 하나의 ID로 문장 전체를 인용하지 않는다" in prompt
        assert "목차 항목이나 제목만 확인되면 그것을 정의 답변으로" in prompt


def test_grounding_prompt_keeps_evidence_but_not_location_metadata():
    request = QaRequest(question="근거는?", chunks=[QaContextChunk(
        chunk_id="chunk-uuid", section_title="metadata-only-title",
        text="문서의 실제 값은 42이다.", page_start=987, page_end=988,
    )])
    for builder in (provider._build_user_prompt, streaming._build_user_prompt):
        prompt = builder(request)
        assert "chunk-uuid" in prompt
        assert "문서의 실제 값은 42이다." in prompt
        assert "metadata-only-title" not in prompt
        assert "987" not in prompt
        assert "988" not in prompt


def test_both_paths_request_verbatim_paragraphs_without_introductions():
    from app.services.qa.prompt_contract import VERBATIM_QUOTE_RULE

    for prompt in (provider._SYSTEM_PROMPT, streaming._SYSTEM_PROMPT):
        assert prompt.endswith(VERBATIM_QUOTE_RULE)
        assert "문서에 있는 문단 번호와 원문 문장만 그대로" in prompt
        assert "소개 설명이나" in prompt
        assert "문서에 없는 단어 정의를 추가하지 않는다" in prompt
        assert "요청한 서로 다른 문단은 각각 인용" in prompt
