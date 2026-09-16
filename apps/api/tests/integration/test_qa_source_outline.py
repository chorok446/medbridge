"""합성 PDF의 추출·검색 출처를 구조화해도 등급/조건과 저장 원문은 그대로다."""

import copy
import uuid

import pymupdf
import pytest
from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.qa import QaMessage
from app.models.search import DocumentChunk
from app.qa_eval.source_bundle_selection import select_source_bundles
from app.qa_eval.source_outline import build_source_outlines
from app.services.qa.context import retrieve
from app.services.qa.provider import QaRequest
from tests.integration.test_qa_api import upload_chunked
from tests.unit.test_qa_evidence_selection import _client
from tests.unit.test_qa_source_outline import assert_lossless


@pytest.mark.parametrize("select_bundle", [False, True])
async def test_outline_preserves_uploaded_grade_rows_without_saving_answers(client, select_bundle):
    labels = ["I", "II", "III", "IV"]
    rows = [f"{label}\n장치의 전압 기준은 {24 * (i + 1)}V이다.\n단, 외부 전원이 필요하다."
            for i, label in enumerate(labels)]
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 60), "장치의 등급 분류", fontname="korea", fontsize=14)
        for i, row in enumerate(rows):
            page.insert_text((50, 140 + i * 100), row, fontname="korea", fontsize=10)
        doc_id = uuid.UUID(await upload_chunked(client, pdf.tobytes()))
    async with get_session_factory()() as db:
        stored = (await db.scalars(select(DocumentChunk).where(
            DocumentChunk.document_id == doc_id,
        ))).all()
        before = {str(c.id): (c.normalized_text, c.content_hash, copy.deepcopy(c.source_refs_json))
                  for c in stored}
        result = await retrieve(db, doc_id, "장치의 분류")
        outlines = build_source_outlines(result.chunks, result.lookup)
        if select_bundle:
            selected = select_source_bundles(
                QaRequest("장치의 분류", result.chunks), result.lookup,
                groups=[[c.chunk_id for c in result.chunks]],
                http_client=_client({"status": "selected", "decisions": {"0": True}}, []),
            )
            assert selected.outlines == outlines
            outlines = selected.outlines
        structured = [o for o in outlines if o.layout == "labelled_sequence"]
        assert len(structured) == 1
        outline = structured[0]
        items = [u for u in outline.units if u.label_text]
        assert [u.label_text for u in items] == labels
        assert [u.text.strip() for u in items] == rows
        assert_lossless(outline, result.lookup[outline.evidence.chunk_id].text)
        assert outline.evidence.source_refs == result.lookup[outline.evidence.chunk_id].source_refs
        assert {r["pageNumber"] for r in outline.evidence.source_refs} == {1}
    async with get_session_factory()() as db:
        stored = (await db.scalars(select(DocumentChunk).where(
            DocumentChunk.document_id == doc_id,
        ))).all()
        assert {str(c.id): (c.normalized_text, c.content_hash, c.source_refs_json)
                for c in stored} == before
        assert await db.scalar(select(func.count()).select_from(QaMessage)) == 0
