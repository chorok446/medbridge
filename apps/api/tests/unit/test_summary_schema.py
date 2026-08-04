"""요약 구조화 출력 파싱·검증 단위 테스트 — 출처 안전 규칙 중심."""

import pytest

from app.models.enums import SummaryArtifactType
from app.services.summary.numbers import (
    extract_number_artifacts,
    extract_population_artifacts,
)
from app.services.summary.provider import (
    ChunkInput,
    DeterministicSummaryProvider,
    DisabledSummaryProvider,
    DocumentRequest,
    GroupRequest,
)
from app.services.summary.schema import ChunkRef, build_artifacts
from app.services.summary.settings import OVERVIEW_MAX_CHARS


def _chunk(cid: str, text: str, page: int = 1) -> ChunkRef:
    return ChunkRef(
        chunk_id=cid,
        source_refs=[
            {
                "pageNumber": page,
                "blockId": f"b-{cid}",
                "bbox": [1.0, 2.0, 3.0, 4.0],
                "readingOrder": 0,
                "sourceMethod": "digital",
            }
        ],
        text=text,
    )


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


class TestOverLimitFields:
    """상한을 넘긴 필드를 통째로 버리면 사용자는 개요가 없는 요약을 오류 없이 받는다.

    중간에서 자르면 안 된다는 판단은 옳다 — 의료 문장에서 "투여 금기다"가 "투여 금"으로
    잘리면 의미가 뒤집힌다. 하지만 그렇다고 항목 전체를 없애면, 남길 수 있었던 문장까지
    함께 사라지고 그 사실이 화면에 전혀 드러나지 않는다. 문장 경계에서 자르면 둘 다
    피할 수 있다.
    """

    def _overview(self, text: str):
        lookup = _lookup("c1")
        structured = {"overview": {"text": text, "sourceChunkIds": ["c1"]}}
        return build_artifacts(structured, lookup, learner_level="nursing_student")

    def test_keeps_whole_sentences_instead_of_dropping_everything(self):
        sentence = "이 약은 신부전 환자에게 투여 금기다. "
        arts = self._overview(sentence * (OVERVIEW_MAX_CHARS // len(sentence) + 3))

        assert arts, "상한을 넘겼다고 개요가 통째로 사라졌다"
        text = arts[0].content_json["text"]
        assert len(text) <= OVERVIEW_MAX_CHARS
        assert text.endswith("금기다."), f"문장 중간에서 잘렸다: ...{text[-20:]}"

    def test_drops_when_there_is_no_sentence_boundary_to_cut_at(self):
        """자를 경계가 없으면 버린다 — 중간 절단으로 의미를 뒤집지 않는다."""
        arts = self._overview("가" * (OVERVIEW_MAX_CHARS + 200))
        assert arts == []

    def test_within_limit_text_is_untouched(self):
        arts = self._overview("짧은 개요다.")
        assert arts[0].content_json["text"] == "짧은 개요다."


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

    def test_scans_whole_document_not_just_the_front(self):
        """상한에 닿아도 문서 앞부분에서 멈추지 않는다.

        실기기: 1,060페이지 교재에서 뽑힌 수치 30개가 전부 2~19페이지(판권지·머리말·목차)
        에서 나왔다. 문서 순서로 훑다가 상한에서 곧바로 return하면 본문에 영영 닿지 않는다.
        """
        lookup = {}
        # 앞쪽 청크: 목차처럼 수치가 빽빽하다. 상한(30)을 확실히 넘기고도 남는 양이라,
        # 앞에서 멈추는 구현이면 본문 청크에는 절대 닿지 못한다.
        for i in range(40):
            lookup[f"front{i}"] = _chunk(
                f"front{i}", f"제{i + 1}장 ... {100 + i}-{200 + i} 쪽"
            )
        # 본문 청크: 진짜 임상 수치
        lookup["body"] = _chunk("body", "목표 혈압은 140 mmHg 미만이며 초기 용량은 250mg이다.")

        values = {
            a.content_json["value"]
            for a in extract_number_artifacts(lookup, start_position=0)
        }
        assert "140 mmHg" in values, "본문 수치가 앞부분에 밀려 잘렸다"
        assert "250mg" in values

    def test_drops_publication_years(self):
        """판권지의 발행 연도는 임상 수치가 아니다."""
        lookup = {
            "c1": _chunk("c1", "1993년 초판, 2022년 6판 발행. 5년 생존율은 70%이다.")
        }
        values = {
            a.content_json["value"]
            for a in extract_number_artifacts(lookup, start_position=0)
        }
        assert "1993년" not in values
        assert "2022년" not in values
        # 임상적 기간·비율은 남는다
        assert "5년" in values
        assert "70%" in values

    def test_does_not_span_line_breaks_or_split_thousands(self):
        """OCR 줄바꿈과 천단위 쉼표에서 값이 망가지지 않는다.

        실기기에서 "3.0\\nmg", "000 ml"(1,000 ml의 뒷동강)이 그대로 저장됐다.
        """
        lookup = {
            "c1": _chunk("c1", "용량은 3.0\nmg 기준이며 총 1,000 ml를 투여한다."),
        }
        values = {
            a.content_json["value"]
            for a in extract_number_artifacts(lookup, start_position=0)
        }
        assert not any("\n" in v for v in values), f"개행을 넘어 매치됐다: {values}"
        assert "000 ml" not in values
        assert "1,000 ml" in values

    def test_spreads_across_the_whole_document(self):
        """뽑힌 수치는 문서 앞뒤에 고르게 퍼진다.

        앞쪽에도 뒤쪽에도 똑같이 좋은 후보가 있으면 앞쪽이 목록을 독점해선 안 된다.
        """
        lookup = {
            f"c{i}": _chunk(f"c{i}", f"용량은 {i + 1} mg이다.") for i in range(200)
        }
        arts = extract_number_artifacts(lookup, start_position=0)
        positions = [int(a.source_chunk_ids[0][1:]) for a in arts]
        assert max(positions) > 150, f"뒤쪽 청크가 전혀 뽑히지 않았다: {max(positions)}"
        assert min(positions) < 50, f"앞쪽 청크가 전혀 뽑히지 않았다: {min(positions)}"

    def test_one_chunk_cannot_monopolize(self):
        """한 청크가 목록 전체를 차지하지 못한다 — 문서 전체가 대표돼야 한다."""
        dense = " ".join(f"{i} mmHg" for i in range(1, 40))
        lookup = {"dense": _chunk("dense", dense), "other": _chunk("other", "용량 750mg")}
        arts = extract_number_artifacts(lookup, start_position=0)
        by_chunk = [a.source_chunk_ids[0] for a in arts]
        assert "other" in by_chunk, "다른 청크가 통째로 밀려났다"


class TestPopulations:
    def test_rejects_table_of_contents_lines(self):
        """목차 줄은 대상 집단이 아니다.

        실기기에서 "쿠싱증후군 555", "Part 10 중환자 /901" 같은 목차 줄이 쪽번호까지
        달린 채 TARGET_POPULATION으로 저장됐다.
        """
        lookup = {
            "c1": _chunk(
                "c1",
                "급성 관동맥 증후군 485\n"
                "쿠싱증후군 555\n"
                "Part 10 중환자 /901\n"
                "발열 환자에 대한 접근 763\n",
            )
        }
        texts = {
            a.content_json["text"]
            for a in extract_population_artifacts(lookup, start_position=0)
        }
        assert texts == set(), f"목차 줄이 저장됐다: {texts}"

    def test_rejects_staff_listing(self):
        """집필진 명단은 대상 집단이 아니다 ('중환자: 이진우'가 실제로 저장됐다)."""
        lookup = {"c1": _chunk("c1", "중환자: 이진우\n호흡기: 김철수\n")}
        arts = extract_population_artifacts(lookup, start_position=0)
        assert arts == []

    def test_keeps_real_population_sentence(self):
        lookup = {
            "c1": _chunk(
                "c1", "이 지침의 대상 환자는 65세 이상 성인 입원 환자로 한정한다."
            )
        }
        arts = extract_population_artifacts(lookup, start_position=0)
        assert len(arts) == 1
        assert "65세 이상" in arts[0].content_json["text"]
        assert arts[0].source_refs


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
