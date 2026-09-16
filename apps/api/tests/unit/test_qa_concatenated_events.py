"""줄바꿈이 빠진 완전한 JSON 이벤트만 읽고 손상·자유 텍스트는 구제하지 않는다."""

import json

import pytest

from app.services.qa.provider import QaRequest
from app.services.qa.settings import MAX_CLAIMS
from app.services.qa.streaming import OpenAICompatibleStreamingQaProvider
from tests.unit.test_qa_streaming import _Token

CLAIM = {"type": "claim", "text": "이 문서의 주제는 손 위생이다.", "sourceChunkIds": ["c1"]}
FINAL = {"type": "final", "answerStatus": "answered", "followUpSuggestions": []}


def encode(event):
    return json.dumps(event, ensure_ascii=False)


def read(content, native, *, split=0):
    fragments = [content] if not split else [content[:split], content[split:]]
    if native:
        frames = [json.dumps({"message": {"content": part}, "done": False})
                  for part in fragments]
        frames.append('{"done":true}')
    else:
        frames = ["data: " + json.dumps({"choices": [{"delta": {"content": part}}]})
                  for part in fragments]
        frames.append("data: [DONE]")
    provider = OpenAICompatibleStreamingQaProvider(
        endpoint="http://127.0.0.1:11434/v1" if native else "https://api.example.com/v1",
        model_name="qwen3:8b", api_key="" if native else "synthetic", is_local=native,
        line_source=lambda *_: iter(frames),
    )
    return list(provider.stream_answer(QaRequest(question="합성 질문", chunks=[]), _Token()))


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("separator", ["", " ", "\t", "\n"])
@pytest.mark.parametrize("split", [0, 17])
def test_complete_adjacent_objects_preserve_content_and_order(native, separator, split):
    assert read(encode(CLAIM) + separator + encode(FINAL), native, split=split) == [CLAIM, FINAL]


@pytest.mark.parametrize("native", [False, True])
def test_braces_and_escaped_quotes_inside_text_are_not_event_boundaries(native):
    claim = {**CLAIM, "text": '합성 문자열은 }{ 와 "type": "final" 및 줄\n바꿈을 포함한다.'}
    assert read(encode(claim) + encode(FINAL), native) == [claim, FINAL]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("content", [
    encode(CLAIM) + encode(FINAL)[:-1],
    encode(CLAIM) + encode(FINAL) + "}",
    "설명: " + encode(CLAIM),
    encode(CLAIM) + " 이 뒤는 자유 텍스트다.",
    "[" + encode(CLAIM) + "," + encode(FINAL) + "]",
    encode(CLAIM) + '{"type":"tool_call","command":"synthetic"}',
    encode(CLAIM) + "null",
])
def test_incomplete_or_non_event_line_is_not_partially_repaired(native, content):
    assert read(content, native) == []


@pytest.mark.parametrize("native", [False, True])
def test_final_stops_following_claims_and_existing_limits_remain(native):
    assert read(encode(FINAL) + encode(CLAIM), native) == [FINAL]
    assert read(encode(CLAIM) * MAX_CLAIMS + encode(FINAL), native) == (
        [CLAIM] * MAX_CLAIMS + [FINAL]
    )
    assert len(read(encode(CLAIM) * (MAX_CLAIMS + 2), native)) <= MAX_CLAIMS
