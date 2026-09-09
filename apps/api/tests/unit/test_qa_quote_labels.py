"""번호 없는 순수 인용만 실제 원문의 문단 표기로 복원한다."""

from copy import deepcopy

import pytest

from app.services.qa.provider import QaContextChunk, QaRequest

SOURCE = "심장은 온몸에 혈액을 보내는 근육 기관이다."
QUESTION = "단어를 설명하고 문단 1부터 문단 12까지 순서대로 원문을 인용해 주세요."


def _chunk(cid="c1", bodies=None):
    bodies = bodies or [SOURCE] * 12
    text = "제목\n\n" + "\n\n".join(
        f"문단 {i}. {body}" for i, body in enumerate(bodies, 1)
    )
    return QaContextChunk(cid, None, text, 1, 2)


def _claim(text=SOURCE, ids=None):
    return {"type": "claim", "text": text, "sourceChunkIds": ids or ["c1"]}


def _restore(events, *, chunks=None, question=QUESTION):
    from app.services.qa.quote_labels import restore_quote_labels

    return restore_quote_labels(QaRequest(question=question, chunks=chunks or [_chunk()]), events)


@pytest.mark.parametrize("with_final", [True, False])
def test_repeated_literal_quotes_keep_all_source_paragraph_labels(with_final):
    events = [_claim() for _ in range(12)]
    if with_final:
        events.append({"type": "final", "answerStatus": "answered"})
    before = deepcopy(events)
    restored = _restore(events)
    assert [e["text"] for e in restored if e["type"] == "claim"] == [
        f"문단 {i}. {SOURCE}" for i in range(1, 13)
    ]
    assert all(e["sourceChunkIds"] == ["c1"] for e in restored if e["type"] == "claim")
    assert events == before
    if with_final:
        assert restored[-1] == events[-1]


def test_canonical_order_and_only_model_cited_matching_sources_are_preserved():
    events = [_claim(f"문단 {i}: {SOURCE}", ["c2"]) for i in reversed(range(1, 13))]
    events.insert(3, _claim())
    out = _restore(events, chunks=[_chunk(), _chunk("c2"), _chunk("uncited")])
    assert [e["text"] for e in out] == [f"문단 {i}. {SOURCE}" for i in range(1, 13)]
    assert all(e["sourceChunkIds"] == ["c1", "c2"] for e in out)


def test_unsupported_explanations_are_not_rewritten_or_removed():
    unsupported = _claim(f"산소를 운반하는 체액이다 (원문: {SOURCE})")
    out = _restore([unsupported, _claim()])
    assert out[0] == unsupported
    assert len(out) == 13


@pytest.mark.parametrize("question", [
    "심장은 무엇을 하나요?",
    "문단 1부터 문단 12까지 설명해 주세요.",
    "문단 1부터 문단 12까지 원문을 인용하지 말아 주세요.",
    "문단 1부터 문단 12까지 원문을 인용해 주지 마세요.",
    "문단 12부터 문단 1까지 원문을 인용해 주세요.",
    "문단 1부터 문단 21까지 원문을 인용해 주세요.",
    "2장 문단 1부터 문단 12까지 원문을 인용해 주세요.",
    "문단 1부터 3까지와 문단 5부터 7까지 원문을 인용해 주세요.",
])
def test_unsupported_or_ambiguous_requests_are_unchanged(question):
    events = [_claim()]
    assert _restore(events, question=question) == events


@pytest.mark.parametrize("status", ["not_found", "insufficient_evidence", "conflicting_evidence"])
def test_model_abstention_and_conflict_are_unchanged(status):
    events = [_claim(), {"type": "final", "answerStatus": status}]
    assert _restore(events) == events


@pytest.mark.parametrize("text,ids", [
    (SOURCE, ["unknown"]),
    (SOURCE, "c1"),
    (None, ["c1"]),
    ("다른 사실을 설명한다.", ["c1"]),
    (f"문단 1: {SOURCE}", ["c1"]),
])
def test_missing_literal_coverage_or_invalid_source_is_unchanged(text, ids):
    events = [_claim(text, ids)]
    assert _restore(events) == events


def test_missing_or_conflicting_source_paragraphs_are_unchanged():
    events = [_claim()]
    assert _restore(events, chunks=[_chunk(bodies=[SOURCE] * 11)]) == events
    differing = [SOURCE] * 11 + ["서로 다른 마지막 문단이다."]
    assert _restore(events, chunks=[_chunk(), _chunk("c2", differing)]) == events
    # 다른 내용의 마지막 문단을 모델이 인용하지 않았을 때 새 사실을 채워 넣지 않는다.
    assert _restore(events, chunks=[_chunk(bodies=differing)]) == events


def test_different_paragraphs_require_each_literal_body_and_correct_source():
    bodies = [f"지표 {i}은 증가하였다." for i in range(1, 13)]
    events = [_claim(body) for body in bodies]
    out = _restore(events, chunks=[_chunk(bodies=bodies)])
    assert [e["text"] for e in out] == [f"문단 {i}. {body}" for i, body in enumerate(bodies, 1)]
    wrong_source = [_claim(body, ["c2"]) for body in bodies]
    assert _restore(wrong_source, chunks=[_chunk(bodies=bodies), _chunk("c2")]) == wrong_source


def test_output_claim_and_text_budgets_are_not_expanded():
    events = [_claim(f"다른 설명 {i}") for i in range(9)] + [_claim()]
    assert _restore(events) == events  # 9 + 12는 MAX_CLAIMS 초과
    long_body = "긴 문장 " * 150
    events = [_claim(long_body)]
    assert _restore(events, chunks=[_chunk(bodies=[long_body] * 12)]) == events
    malformed = [_claim(), _claim(123)]
    assert _restore(malformed) == malformed
    medium_body = "근거 문장 " * 65 + "."
    events = [_claim(medium_body)]
    assert _restore(events, chunks=[_chunk(bodies=[medium_body] * 12)]) == events


def test_nested_labels_or_unfinished_paragraphs_are_unchanged():
    events = [_claim()]
    nested = _chunk()
    nested.text = nested.text.replace("\n\n문단", "\n문단")
    assert _restore(events, chunks=[nested]) == events
    assert _restore(events, chunks=[_chunk(bodies=[SOURCE[:-5]] * 12)]) == events


def test_whitespace_and_optional_terminal_period_do_not_lose_labels():
    out = _restore([_claim("  " + SOURCE.removesuffix(".") + "  ")])
    assert len(out) == 12
    assert out[-1]["text"] == f"문단 12. {SOURCE}"


@pytest.mark.parametrize("separator", ["부터", "~", "-"])
def test_other_explicit_ranges_copy_only_requested_source_labels(separator):
    out = _restore([_claim()], question=f"문단 3{separator}문단 5까지 원문을 인용해주세요.")
    assert [e["text"] for e in out] == [f"문단 {i}. {SOURCE}" for i in (3, 4, 5)]


def test_native_provider_restores_labels_after_the_successful_attempt(monkeypatch):
    from app.services.qa.streaming import OpenAICompatibleStreamingQaProvider

    provider = OpenAICompatibleStreamingQaProvider(
        endpoint="http://127.0.0.1:11434/v1", model_name="qwen3:8b", api_key="", is_local=True,
    )
    events = [_claim() for _ in range(12)] + [{"type": "final", "answerStatus": "answered"}]
    monkeypatch.setattr(provider, "_stream_attempt", lambda *a, **kw: iter(events))

    class Token:
        def is_cancelled(self):
            return False

    out = list(provider.stream_answer(QaRequest(question=QUESTION, chunks=[_chunk()]), Token()))
    assert [e["text"] for e in out[:-1]] == [f"문단 {i}. {SOURCE}" for i in range(1, 13)]
    assert out[-1] == events[-1]


def test_external_streaming_does_not_buffer_or_restore(monkeypatch):
    from app.services.qa.streaming import OpenAICompatibleStreamingQaProvider

    provider = OpenAICompatibleStreamingQaProvider(
        endpoint="https://api.example.com/v1", model_name="external",
        api_key="test", is_local=False,
    )
    events = [_claim(), {"type": "final", "answerStatus": "answered"}]
    monkeypatch.setattr(provider, "_stream_attempt", lambda *a, **kw: iter(events))
    request = QaRequest(question=QUESTION, chunks=[_chunk()])
    assert list(provider.stream_answer(request, None)) == events


def test_cancelled_attempt_does_not_restore_or_replay_quotes(monkeypatch):
    from app.services.qa.streaming import OpenAICompatibleStreamingQaProvider

    provider = OpenAICompatibleStreamingQaProvider(
        endpoint="http://127.0.0.1:11434/v1", model_name="qwen3:8b", api_key="", is_local=True,
    )

    class Token:
        cancelled = False

        def is_cancelled(self):
            return self.cancelled

    token = Token()

    def attempt(*a, **kw):
        yield _claim()
        token.cancelled = True

    monkeypatch.setattr(provider, "_stream_attempt", attempt)
    request = QaRequest(question=QUESTION, chunks=[_chunk()])
    assert list(provider.stream_answer(request, token)) == []
