"""요약 구조화 출력 파싱·검증 단위 테스트 — 출처 안전 규칙 중심."""

import pytest

from app.models.enums import SummaryArtifactType
from app.services.summary.numbers import extract_number_artifacts
from app.services.summary.provider import (
    ChunkInput,
    DeterministicSummaryProvider,
    DisabledSummaryProvider,
    DocumentRequest,
    GroupRequest,
)
from app.services.summary.schema import ChunkRef, build_artifacts


def _lookup(*ids: str) -> dict[str, ChunkRef]:
    return {
        cid: ChunkRef(
            chunk_id=cid,
            source_refs=[
                {
                    "pageNumber": i + 1,
                    "blockId": f"b{i}",
                    "bbox": [1.0, 2.0, 3.0, 4.0],
                    "readingOrder": i,
                    "sourceMethod": "digital",
                }
            ],
            text=f"청크 {cid} 본문",
        )
        for i, cid in enumerate(ids)
    }


class TestBuildArtifacts:
    def test_reconstructs_source_refs_from_chunks_not_model(self):
        lookup = _lookup("c1")
        # 모델이 엉뚱한 page/bbox를 줘도 무시하고 저장된 chunk의 source_refs를 쓴다
        structured = {
            "overview": {
                "text": "개요",
                "sourceChunkIds": ["c1"],
                "page": 999,
                "bbox": [111, 222, 333, 444],
            }
        }
        arts = build_artifacts(structured, lookup, learner_level="nursing_student")
        assert len(arts) == 1
        ref = arts[0].source_refs[0]
        assert ref["pageNumber"] == 1  # 모델의 999가 아니라 chunk 저장값
        assert ref["bbox"] == [1.0, 2.0, 3.0, 4.0]

    def test_rejects_unknown_chunk_ids(self):
        lookup = _lookup("c1")
        structured = {"overview": {"text": "개요", "sourceChunkIds": ["does-not-exist"]}}
        arts = build_artifacts(structured, lookup, learner_level="nursing_student")
        assert arts == []  # 알 수 없는 chunk id만 있으면 출처 없음 → 저장 안 함

    def test_mixes_valid_and_invalid_keeps_only_valid(self):
        lookup = _lookup("c1", "c2")
        structured = {"overview": {"text": "개요", "sourceChunkIds": ["c1", "bogus", "c2"]}}
        arts = build_artifacts(structured, lookup, learner_level="nursing_student")
        assert arts[0].source_chunk_ids == ["c1", "c2"]

    def test_drops_empty_content(self):
        lookup = _lookup("c1")
        structured = {
            "overview": {"text": "   ", "sourceChunkIds": ["c1"]},
            "sections": [{"title": "t", "summary": "", "sourceChunkIds": ["c1"]}],
        }
        arts = build_artifacts(structured, lookup, learner_level="nursing_student")
        assert arts == []

    def test_dedupes_sections_and_concepts(self):
        lookup = _lookup("c1")
        structured = {
            "sections": [
                {"title": "A", "summary": "같은 내용", "sourceChunkIds": ["c1"]},
                {"title": "A", "summary": "같은 내용", "sourceChunkIds": ["c1"]},
            ],
            "keyConcepts": [
                {"term": "심장", "explanation": "x", "sourceChunkIds": ["c1"]},
                {"term": "심장", "explanation": "y", "sourceChunkIds": ["c1"]},
            ],
        }
        arts = build_artifacts(structured, lookup, learner_level="nursing_student")
        sections = [a for a in arts if a.artifact_type == SummaryArtifactType.SECTION_SUMMARY]
        concepts = [a for a in arts if a.artifact_type == SummaryArtifactType.KEY_CONCEPT]
        assert len(sections) == 1
        assert len(concepts) == 1

    def test_prerequisite_general_background_excluded(self):
        lookup = _lookup("c1")
        structured = {
            "prerequisites": [
                {"concept": "해부학", "whyNeeded": "필요", "sourceType": "document",
                 "sourceChunkIds": ["c1"]},
                {"concept": "일반상식", "whyNeeded": "배경", "sourceType": "general_background",
                 "sourceChunkIds": ["c1"]},
            ]
        }
        arts = build_artifacts(structured, lookup, learner_level="nursing_student")
        pre = [a for a in arts if a.artifact_type == SummaryArtifactType.PREREQUISITE]
        assert len(pre) == 1
        assert pre[0].title == "해부학"

    def test_model_supplied_numbers_ignored(self):
        # 모델이 importantNumbers/targetPopulations를 줘도 artifact로 만들지 않는다(§7)
        lookup = _lookup("c1")
        structured = {
            "importantNumbers": [{"value": "999mg", "sourceChunkIds": ["c1"]}],
            "targetPopulations": [{"text": "환자군", "sourceChunkIds": ["c1"]}],
        }
        arts = build_artifacts(structured, lookup, learner_level="nursing_student")
        assert arts == []


class TestNumbers:
    def test_extracts_only_numbers_present_in_source(self):
        lookup = {
            "c1": ChunkRef(
                chunk_id="c1",
                source_refs=[{"pageNumber": 1, "blockId": "b", "bbox": [0, 0, 1, 1],
                              "readingOrder": 0, "sourceMethod": "digital"}],
                text="투여량은 500mg이며 대상 환자는 65세 이상이다. 유효율 80%.",
            )
        }
        arts = extract_number_artifacts(lookup, start_position=0)
        values = {a.content_json["value"] for a in arts}
        assert "500mg" in values
        assert "80%" in values
        assert "65세" in values
        for a in arts:
            # 추출된 수치는 반드시 원문에 존재한다
            assert a.content_json["value"] in lookup["c1"].text
            assert a.source_refs  # 출처 있음


class TestProviders:
    def test_disabled_raises(self):
        p = DisabledSummaryProvider()
        assert p.available is False
        with pytest.raises(RuntimeError):
            p.summarize_document(
                DocumentRequest(group_summaries=[], learner_level="nursing_student", language="ko")
            )

    def test_openai_compatible_uses_injected_client_no_network(self):
        import json as _json

        from app.services.summary.provider import OpenAICompatibleSummaryProvider

        calls: list[str] = []

        def fake_http(url, payload, api_key):
            calls.append(url)
            assert api_key == "k"  # 키가 전달되지만 로그·응답엔 안 남는다
            content = _json.dumps({"summary": "요약", "sourceChunkIds": ["c1"]})
            return _json.dumps({"choices": [{"message": {"content": content}}]})

        p = OpenAICompatibleSummaryProvider(
            endpoint="https://api.example.com/v1",
            model_name="gpt-x",
            api_key="k",
            is_local=False,
            http_client=fake_http,
        )
        assert p.available is True
        gs = p.summarize_group(
            GroupRequest(
                group_id="g0",
                section_title=None,
                chunks=[ChunkInput("c1", None, "본문", 1, 1)],
                learner_level="nursing_student",
                language="ko",
            )
        )
        assert gs.source_chunk_ids == ["c1"]
        assert calls and calls[0].endswith("/chat/completions")

    def test_deterministic_structured_output_uses_real_chunk_ids(self):
        p = DeterministicSummaryProvider()
        gs = p.summarize_group(
            GroupRequest(
                group_id="g0",
                section_title="순환계",
                chunks=[ChunkInput("c1", "순환계", "심장은 중요하다.", 1, 1)],
                learner_level="nursing_student",
                language="ko",
            )
        )
        assert gs.source_chunk_ids == ["c1"]
        doc = p.summarize_document(
            DocumentRequest(
                group_summaries=[gs], learner_level="nursing_student", language="ko"
            )
        )
        assert doc["overview"]["sourceChunkIds"] == ["c1"]
        assert doc["studyCautions"] == []
