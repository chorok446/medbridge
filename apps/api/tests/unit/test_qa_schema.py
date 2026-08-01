"""Q&A 출처 검증·공급자 단위 테스트."""

import json

import pytest

from app.models.enums import QaClaimVerification
from app.services.qa.context import QaChunkRef
from app.services.qa.provider import (
    DeterministicQaProvider,
    DisabledQaProvider,
    OpenAICompatibleQaProvider,
    QaContextChunk,
    QaRequest,
)
from app.services.qa.schema import verify


def _lookup(*specs) -> dict[str, QaChunkRef]:
    """specs: (chunk_id, text) 튜플들."""
    out: dict[str, QaChunkRef] = {}
    for i, (cid, text) in enumerate(specs):
        out[cid] = QaChunkRef(
            chunk_id=cid,
            section_title=f"섹션{i}",
            text=text,
            content_hash=f"h{i}",
            source_refs=[
                {
                    "pageNumber": i + 1,
                    "blockId": f"b{i}",
                    "bbox": [1.0, 2.0, 3.0, 4.0],
                    "readingOrder": i,
                    "sourceMethod": "digital",
                }
            ],
        )
    return out


class TestVerify:
    def test_supported_claim_reconstructs_refs_from_chunks(self):
        lookup = _lookup(("c1", "심장은 혈액을 보낸다"))
        out = verify(
            {
                "answer": "심장은 혈액을 보냅니다.",
                "answerStatus": "answered",
                "claims": [{"text": "심장은 혈액을 보낸다", "sourceChunkIds": ["c1"]}],
            },
            lookup,
            had_results=True,
        )
        assert out.answer_status == "completed"
        assert len(out.claims) == 1
        c = out.claims[0]
        assert c.verification_status == QaClaimVerification.SUPPORTED
        # page/bbox는 저장된 chunk에서 재구성 (모델이 아니라)
        assert c.source_refs[0]["pageNumber"] == 1
        assert c.source_refs[0]["bbox"] == [1.0, 2.0, 3.0, 4.0]

    def test_unknown_chunk_id_rejected(self):
        lookup = _lookup(("c1", "본문"))
        out = verify(
            {"answer": "x", "answerStatus": "answered",
             "claims": [{"text": "주장", "sourceChunkIds": ["nope"]}]},
            lookup,
            had_results=True,
        )
        # 유효 출처 없음 → 지원 주장 0 → insufficient_evidence, unsupported claim 제거
        assert out.answer_status == "insufficient_evidence"
        assert out.claims == []

    def test_claim_without_source_is_unsupported(self):
        lookup = _lookup(("c1", "본문"))
        out = verify(
            {"answer": "x", "answerStatus": "answered",
             "claims": [{"text": "출처 없는 주장", "sourceChunkIds": []}]},
            lookup,
            had_results=True,
        )
        assert out.answer_status == "insufficient_evidence"

    def test_number_not_in_source_is_unsupported(self):
        lookup = _lookup(("c1", "용량은 500mg이다"))
        out = verify(
            {"answer": "x", "answerStatus": "answered",
             "claims": [{"text": "용량은 999mg이다", "sourceChunkIds": ["c1"]}]},
            lookup,
            had_results=True,
        )
        # 999mg는 원문에 없음 → unsupported → 지원 0 → insufficient
        assert out.answer_status == "insufficient_evidence"

    def test_number_boundary_not_substring(self):
        # 50mg 주장이 원문의 150mg에 substring으로 매치되면 안 된다
        lookup = _lookup(("c1", "투여량은 150mg이다"))
        out = verify(
            {"answer": "x", "answerStatus": "answered",
             "claims": [{"text": "투여량은 50mg이다", "sourceChunkIds": ["c1"]}]},
            lookup,
            had_results=True,
        )
        assert out.answer_status == "insufficient_evidence"

    def test_percent_number_boundary_not_substring(self):
        # "50%" 주장이 원문의 "500명" 안 "50"에 substring으로 매치되면 안 된다
        lookup = _lookup(("c1", "대상 500명을 관찰했다"))
        out = verify(
            {"answer": "x", "answerStatus": "answered",
             "claims": [{"text": "대상의 50%가 개선되었다", "sourceChunkIds": ["c1"]}]},
            lookup,
            had_results=True,
        )
        assert out.answer_status == "insufficient_evidence"

    def test_comma_formatted_number_matches(self):
        lookup = _lookup(("c1", "대상은 1000명이다"))
        out = verify(
            {"answer": "x", "answerStatus": "answered",
             "claims": [{"text": "대상은 1,000명이다", "sourceChunkIds": ["c1"]}]},
            lookup,
            had_results=True,
        )
        assert out.answer_status == "completed"

    def test_lexically_ungrounded_claim_rejected(self):
        # 근거 청크와 공유 어휘가 사실상 없는 날조 주장은 supported로 저장하지 않는다
        lookup = _lookup(("c1", "심장은 혈액을 온몸에 보내는 근육 기관이다"))
        fabricated = "간은 담즙을 분비하고 해독을 담당한다"
        out = verify(
            {"answer": "x", "answerStatus": "answered",
             "claims": [{"text": fabricated, "sourceChunkIds": ["c1"]}]},
            lookup,
            had_results=True,
        )
        assert out.answer_status == "insufficient_evidence"

    def test_number_in_source_supported(self):
        lookup = _lookup(("c1", "용량은 500mg이다"))
        out = verify(
            {"answer": "x", "answerStatus": "answered",
             "claims": [{"text": "용량은 500mg이다", "sourceChunkIds": ["c1"]}]},
            lookup,
            had_results=True,
        )
        assert out.answer_status == "completed"
        assert out.claims[0].verification_status == QaClaimVerification.SUPPORTED

    def test_no_results_is_not_found(self):
        out = verify(
            {"answer": "", "answerStatus": "not_found", "claims": []}, {}, had_results=False
        )
        assert out.answer_status == "not_found"

    def test_conflicting_requires_two_valid_sources(self):
        lookup = _lookup(("c1", "A는 참이다"), ("c2", "A는 거짓이다"))
        out = verify(
            {
                "answer": "상충",
                "answerStatus": "conflicting_evidence",
                "claims": [
                    {"text": "A는 참이다", "sourceChunkIds": ["c1"]},
                    {"text": "A는 거짓이다", "sourceChunkIds": ["c2"]},
                ],
            },
            lookup,
            had_results=True,
        )
        assert out.answer_status == "conflicting_evidence"
        assert all(c.verification_status == QaClaimVerification.CONFLICTING for c in out.claims)

    def test_conflict_detected_without_model_final_hint(self):
        # 모델이 상충을 명시하지 않아도(answered/누락) 양쪽 지원 주장이 같은 대상에 상반된
        # 극성을 보이면 서버가 conflicting_evidence로 확정한다(상반 근거를 completed로 노출 방지).
        c1 = "초기 연구에서는 이 요법이 사망 위험을 감소시킨다고 보고하였다"
        c2 = "후속 연구에서는 이 요법이 사망 위험에 영향을 주지 않았다고 보고하였다"
        lookup = _lookup(("c1", c1), ("c2", c2))
        out = verify(
            {"answer": "", "answerStatus": "answered",
             "claims": [{"text": c1, "sourceChunkIds": ["c1"]},
                        {"text": c2, "sourceChunkIds": ["c2"]}]},
            lookup, had_results=True,
        )
        assert out.answer_status == "conflicting_evidence"
        assert all(c.verification_status == QaClaimVerification.CONFLICTING for c in out.claims)

    def test_no_false_conflict_for_unrelated_claims(self):
        # 부정 극성 차이가 없는 무관한 다중 주장은 상충으로 오탐하지 않는다.
        c1 = "심장은 혈액을 온몸으로 보낸다"
        c2 = "성인의 정상 안정 시 심박수는 분당 범위에 있다"
        lookup = _lookup(("c1", c1), ("c2", c2))
        out = verify(
            {"answer": "x", "answerStatus": "answered",
             "claims": [{"text": c1, "sourceChunkIds": ["c1"]},
                        {"text": c2, "sourceChunkIds": ["c2"]}]},
            lookup, had_results=True,
        )
        assert out.answer_status == "completed"

    def test_followups_capped_at_three(self):
        lookup = _lookup(("c1", "본문 내용"))
        out = verify(
            {"answer": "x", "answerStatus": "answered",
             "claims": [{"text": "본문 내용", "sourceChunkIds": ["c1"]}],
             "followUpSuggestions": ["q1", "q2", "q3", "q4", "q5"]},
            lookup,
            had_results=True,
        )
        assert len(out.followups) == 3


class TestConflictDetectorAndReasons:
    C1 = "초기 연구에서는 이 요법이 사망 위험을 감소시킨다고 보고하였다"
    C2 = "후속 연구에서는 이 요법이 사망 위험에 영향을 주지 않았다고 보고하였다"

    def test_claims_conflict_needs_negation_divergence_and_shared_subject(self):
        from app.services.qa.schema import claims_conflict

        assert claims_conflict([self.C1, self.C2])  # 극성 반대 + 주제 겹침
        assert not claims_conflict([self.C1])  # 단일 주장
        # 부정 극성 차이가 없으면 상충 아님
        assert not claims_conflict(["약은 통증을 줄인다", "약은 효과가 있다고 보고되었다"])
        # 부정 극성은 다르지만 주제 어휘가 거의 안 겹치면 상충 아님(오탐 방지)
        assert not claims_conflict(["심장은 혈액을 보낸다", "부작용은 보고되지 않았다"])

    def test_classify_claim_event_reason_codes(self):
        from app.services.qa.schema import (
            REJECT_NO_SOURCE,
            REJECT_NOT_GROUNDED,
            REJECT_NUMBER_ABSENT,
            classify_claim_event,
        )

        lookup = _lookup(("c1", "심장은 혈액을 온몸으로 보낸다"))
        vc, reason = classify_claim_event(
            {"text": "심장은 혈액을 보낸다", "sourceChunkIds": ["c1"]}, lookup, claim_index=0
        )
        assert vc is not None and reason is None
        _, r_src = classify_claim_event(
            {"text": "심장은 혈액을 보낸다", "sourceChunkIds": ["zzz"]}, lookup, claim_index=0
        )
        assert r_src == REJECT_NO_SOURCE
        _, r_num = classify_claim_event(
            {"text": "심장은 500 단위를 보낸다", "sourceChunkIds": ["c1"]}, lookup, claim_index=0
        )
        assert r_num == REJECT_NUMBER_ABSENT
        _, r_grd = classify_claim_event(
            {"text": "무관한 날조 주장이다", "sourceChunkIds": ["c1"]}, lookup, claim_index=0
        )
        assert r_grd == REJECT_NOT_GROUNDED


class TestProviders:
    def test_disabled_raises(self):
        p = DisabledQaProvider()
        assert p.available is False
        with pytest.raises(RuntimeError):
            p.answer(QaRequest(question="q", chunks=[]))

    def test_deterministic_uses_real_chunk_ids(self):
        p = DeterministicQaProvider()
        out = p.answer(
            QaRequest(
                question="심장?",
                chunks=[QaContextChunk("c1", "순환계", "심장은 혈액을 보낸다", 1, 1)],
            )
        )
        assert out["answerStatus"] == "answered"
        assert out["claims"][0]["sourceChunkIds"] == ["c1"]

    def test_deterministic_empty_chunks_not_found(self):
        p = DeterministicQaProvider()
        out = p.answer(QaRequest(question="q", chunks=[]))
        assert out["answerStatus"] == "not_found"

    def test_openai_compatible_uses_injected_client(self):
        calls = []

        def fake_http(url, payload, api_key):
            calls.append(url)
            assert api_key == "k"
            content = json.dumps(
                {"answer": "a", "answerStatus": "answered",
                 "claims": [{"text": "t", "sourceChunkIds": ["c1"]}]}
            )
            return json.dumps({"choices": [{"message": {"content": content}}]})

        p = OpenAICompatibleQaProvider(
            endpoint="https://api.example.com/v1",
            model_name="gpt-x",
            api_key="k",
            is_local=False,
            http_client=fake_http,
        )
        assert p.available is True
        out = p.answer(
            QaRequest(question="q", chunks=[QaContextChunk("c1", None, "본문", 1, 1)])
        )
        assert out["claims"][0]["sourceChunkIds"] == ["c1"]
        assert calls[0].endswith("/chat/completions")

    def test_openai_compatible_bad_json_raises(self):
        from app.services.summary.endpoint import SummaryNetworkError

        def fake_http(url, payload, api_key):
            return json.dumps({"choices": [{"message": {"content": "not json"}}]})

        p = OpenAICompatibleQaProvider(
            endpoint="https://api.example.com/v1",
            model_name="gpt-x",
            api_key="k",
            is_local=False,
            http_client=fake_http,
        )
        with pytest.raises(SummaryNetworkError):
            p.answer(QaRequest(question="q", chunks=[QaContextChunk("c1", None, "t", 1, 1)]))
