"""OCR 통합 테스트 — 실제 Tesseract(가능 시) + FakeEngine(제어 경로)."""

import uuid

import pytest
from sqlalchemy import func, select

from app.db.session import get_session_factory
from app.models.enums import OcrRunStatus
from app.models.extraction import DocumentBlock, DocumentPage, DocumentWord
from app.models.ocr import OcrRun
from app.services.extraction.ocr import OcrResult, OcrWord
from app.services.ocr import service as ocr_service
from tests import extraction_fixtures as fx
from tests.integration.conftest import drain_jobs

real_engine = ocr_service.engine()
needs_tesseract = pytest.mark.skipif(
    not real_engine.available, reason="local tesseract not available"
)


def upload_kwargs(data: bytes, filename: str = "scan.pdf"):
    return {"files": {"file": (filename, data, "application/pdf")}}


async def upload_extracted(client, data: bytes) -> dict:
    res = await client.post("/api/documents", **upload_kwargs(data))
    assert res.status_code == 201, res.text
    await drain_jobs()
    return (await client.get(f"/api/documents/{res.json()['data']['id']}")).json()["data"]


class FakeEngine:
    """제어 경로 테스트용 — 페이지마다 지정된 결과/예외를 반환."""

    def __init__(self, result: OcrResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[int] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def version(self) -> str:
        return "fake-1.0"

    def recognize_page(self, pdf_path, page_number, *, language="kor+eng", dpi=0):
        self.calls.append(page_number)
        if self.error:
            raise self.error
        result = self.result or OcrResult(page_number=page_number)
        result.page_number = page_number
        return result


def fake_words(texts: list[str], conf: float = 0.9) -> list[OcrWord]:
    return [
        OcrWord(
            bbox=(50.0 + i * 60, 100.0, 100.0 + i * 60, 120.0),
            text=t,
            confidence=conf,
            block_index=1,
            paragraph_index=1,
            line_index=1,
            word_index=i + 1,
        )
        for i, t in enumerate(texts)
    ]


class TestOcrRealEngine:
    @needs_tesseract
    async def test_full_scanned_korean_document(self, client):
        doc = await upload_extracted(client, fx.scanned_korean_clear())
        assert doc["processingStatus"] == "ocr_required"

        res = await client.post(f"/api/documents/{doc['id']}/ocr")
        assert res.status_code == 200
        assert res.json()["data"]["started"] is True
        await drain_jobs()

        page = (
            await client.get(f"/api/documents/{doc['id']}/pages/1/ocr-result")
        ).json()["data"]
        assert page["ocrStatus"] in ("ocr_completed", "ocr_low_confidence")
        assert "보내는" in page["text"] or "빨라진다" in page["text"]

        # 문서 상태 상향 + 단어 출처
        after = (await client.get(f"/api/documents/{doc['id']}")).json()["data"]
        assert after["processingStatus"] == "extracted"
        async with get_session_factory()() as session:
            ocr_words = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentWord)
                    .join(DocumentPage, DocumentPage.id == DocumentWord.page_id)
                    .where(
                        DocumentPage.document_id == uuid.UUID(doc["id"]),
                        DocumentWord.source_method == "ocr",
                    )
                )
            ).scalar_one()
        assert ocr_words > 0

        # 블록이 생겨 기존 하이라이트 경로로 조회 가능
        blocks = (
            await client.get(f"/api/documents/{doc['id']}/pages/1/blocks")
        ).json()["data"]
        assert any(b["text"].strip() for b in blocks)
        for b in blocks:
            assert b["x0"] <= b["x1"] and b["y0"] <= b["y1"]

    @needs_tesseract
    async def test_mixed_document_digital_untouched(self, client):
        doc = await upload_extracted(client, fx.mixed_digital_and_scanned())
        assert doc["processingStatus"] == "partially_extracted"

        async with get_session_factory()() as session:
            digital_before = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentWord)
                    .join(DocumentPage, DocumentPage.id == DocumentWord.page_id)
                    .where(
                        DocumentPage.document_id == uuid.UUID(doc["id"]),
                        DocumentPage.page_number == 1,
                        DocumentWord.source_method == "digital",
                    )
                )
            ).scalar_one()

        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        status = (await client.get(f"/api/documents/{doc['id']}/ocr-status")).json()["data"]
        assert status["remainingOcrPages"] == []
        # 디지털 페이지 1은 OCR 대상이 아니었고 단어 수 불변
        async with get_session_factory()() as session:
            digital_after = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentWord)
                    .join(DocumentPage, DocumentPage.id == DocumentWord.page_id)
                    .where(
                        DocumentPage.document_id == uuid.UUID(doc["id"]),
                        DocumentPage.page_number == 1,
                        DocumentWord.source_method == "digital",
                    )
                )
            ).scalar_one()
            page1 = (
                await session.execute(
                    select(DocumentPage).where(
                        DocumentPage.document_id == uuid.UUID(doc["id"]),
                        DocumentPage.page_number == 1,
                    )
                )
            ).scalar_one()
        assert digital_after == digital_before
        assert page1.extraction_method == "digital"

    @needs_tesseract
    async def test_numbers_and_units_preserved(self, client):
        doc = await upload_extracted(client, fx.scanned_english_clear())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        text = (
            await client.get(f"/api/documents/{doc['id']}/pages/1/ocr-result")
        ).json()["data"]["text"]
        for token in ("120/80", "mmHg", "45.5%"):
            assert token in text


class TestOcrControlPaths:
    async def test_start_requires_targets(self, client, monkeypatch):
        monkeypatch.setattr(ocr_service, "engine", lambda: FakeEngine())
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        res = await client.post(f"/api/documents/{doc['id']}/ocr")
        assert res.status_code == 200
        assert res.json()["data"]["started"] is False  # digital 문서: 대상 없음

    async def test_invalid_page_number_rejected(self, client, monkeypatch):
        monkeypatch.setattr(ocr_service, "engine", lambda: FakeEngine())
        doc = await upload_extracted(client, fx.single_column_korean(pages=1))
        res = await client.post(
            f"/api/documents/{doc['id']}/ocr", json={"pages": [99]}
        )
        assert res.status_code == 422

    async def test_empty_result_marks_ocr_empty(self, client, monkeypatch):
        fake = FakeEngine(result=OcrResult(page_number=1, words=[]))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        page = (
            await client.get(f"/api/documents/{doc['id']}/pages/1/ocr-result")
        ).json()["data"]
        assert page["ocrStatus"] == "ocr_empty"
        assert page["qualityNotice"] is True

    async def test_low_confidence_flagged(self, client, monkeypatch):
        fake = FakeEngine(
            result=OcrResult(
                page_number=1,
                words=fake_words(["흐린", "글자", "테스트", "문장"], conf=0.3),
                mean_confidence=0.3,
            )
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        status = (await client.get(f"/api/documents/{doc['id']}/ocr-status")).json()["data"]
        assert status["lowConfidencePages"] == [1]

    async def test_engine_failure_marks_failed_and_retry_works(self, client, monkeypatch):
        fake = FakeEngine(error=RuntimeError("boom"))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        status = (await client.get(f"/api/documents/{doc['id']}/ocr-status")).json()["data"]
        assert status["failed"] == 1

        # 재시도 (성공 엔진으로 교체)
        fake2 = FakeEngine(
            result=OcrResult(page_number=1, words=fake_words(["재시도", "성공", "결과", "확인"]))
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake2)
        res = await client.post(f"/api/documents/{doc['id']}/ocr/retry")
        assert res.status_code == 200
        await drain_jobs()
        page = (
            await client.get(f"/api/documents/{doc['id']}/pages/1/ocr-result")
        ).json()["data"]
        assert page["ocrStatus"] == "ocr_completed"
        assert "재시도" in page["text"]

    async def test_cancel_marks_pending_cancelled(self, client, monkeypatch):
        import asyncio

        release = asyncio.Event()

        class SlowEngine(FakeEngine):
            def recognize_page(self, pdf_path, page_number, *, language="kor+eng", dpi=0):
                import time

                time.sleep(0.3)
                return super().recognize_page(
                    pdf_path, page_number, language=language, dpi=dpi
                )

        fake = SlowEngine(
            result=OcrResult(page_number=1, words=fake_words(["a", "b", "c", "d"]))
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.scanned_page_doc(text_pages=0, scanned_pages=3))
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await asyncio.sleep(0.05)
        res = await client.post(f"/api/documents/{doc['id']}/ocr/cancel")
        assert res.status_code == 200
        await drain_jobs()
        _ = release
        async with get_session_factory()() as session:
            statuses = [
                r[0]
                for r in (
                    await session.execute(
                        select(DocumentPage.ocr_status).where(
                            DocumentPage.document_id == uuid.UUID(doc["id"])
                        )
                    )
                ).all()
            ]
        assert OcrRunStatus.OCR_CANCELLED.value in statuses

    async def test_ocr_result_replaced_atomically(self, client, monkeypatch):
        """재실행 시 OCR 행만 교체되고 중복이 없다."""
        fake = FakeEngine(
            result=OcrResult(page_number=1, words=fake_words(["첫번째", "결과", "테스트", "확인"]))
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        fake2 = FakeEngine(
            result=OcrResult(page_number=1, words=fake_words(["두번째", "결과", "교체", "확인"]))
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake2)
        await client.post(f"/api/documents/{doc['id']}/ocr", json={"pages": [1]})
        await drain_jobs()

        async with get_session_factory()() as session:
            words = (
                await session.execute(
                    select(DocumentWord.text)
                    .join(DocumentPage, DocumentPage.id == DocumentWord.page_id)
                    .where(
                        DocumentPage.document_id == uuid.UUID(doc["id"]),
                        DocumentWord.source_method == "ocr",
                    )
                )
            ).all()
            texts = [w[0] for w in words]
            runs = (
                await session.execute(
                    select(func.count())
                    .select_from(OcrRun)
                    .where(OcrRun.document_id == uuid.UUID(doc["id"]))
                )
            ).scalar_one()
        assert "두번째" in texts and "첫번째" not in texts
        assert len(texts) == 4  # 교체, 누적 아님
        assert runs == 2  # 실행 기록은 누적 보존

    async def test_rerun_does_not_accumulate_text_and_word_count(self, client, monkeypatch):
        """재실행해도 normalized_text·word_count가 누적되지 않는다 (Codex H2)."""
        fake = FakeEngine(
            result=OcrResult(page_number=1, words=fake_words(["누적", "검증", "단어", "넷"]))
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        for _ in range(3):
            await client.post(f"/api/documents/{doc['id']}/ocr", json={"pages": [1]})
            await drain_jobs()
        async with get_session_factory()() as session:
            page = (
                await session.execute(
                    select(DocumentPage).where(
                        DocumentPage.document_id == uuid.UUID(doc["id"])
                    )
                )
            ).scalar_one()
        assert page.word_count == 4
        assert page.normalized_text.count("누적") == 1

    async def test_empty_result_keeps_document_unresolved(self, client, monkeypatch):
        """ocr_empty 페이지만 있으면 extracted로 승격되지 않는다 (Codex M6)."""
        fake = FakeEngine(result=OcrResult(page_number=1, words=[]))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        after = (await client.get(f"/api/documents/{doc['id']}")).json()["data"]
        assert after["processingStatus"] == "ocr_required"

    async def test_duplicate_start_blocked_while_queued(self, client, monkeypatch):
        """큐 대기 중 중복 시작은 거부된다 (Codex H3)."""
        import asyncio

        class SlowEngine(FakeEngine):
            def recognize_page(self, pdf_path, page_number, *, language="kor+eng", dpi=0):
                import time

                time.sleep(0.3)
                return super().recognize_page(
                    pdf_path, page_number, language=language, dpi=dpi
                )

        fake = SlowEngine(
            result=OcrResult(page_number=1, words=fake_words(["중복", "시작", "차단", "확인"]))
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        first = await client.post(f"/api/documents/{doc['id']}/ocr")
        assert first.json()["data"]["started"] is True
        await asyncio.sleep(0.05)
        second = await client.post(f"/api/documents/{doc['id']}/ocr", json={"pages": [1]})
        assert second.json()["data"]["started"] is False
        await drain_jobs()

    async def test_status_scoped_to_latest_job(self, client, monkeypatch):
        """진행률은 최근 잡 범위만 센다 — 과거 완료 페이지 합산 금지 (Codex M8)."""
        fake = FakeEngine(
            result=OcrResult(page_number=1, words=fake_words(["범위", "검증", "단어", "넷"]))
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.scanned_page_doc(text_pages=0, scanned_pages=3))
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        await client.post(f"/api/documents/{doc['id']}/ocr", json={"pages": [1]})
        await drain_jobs()
        status = (await client.get(f"/api/documents/{doc['id']}/ocr-status")).json()["data"]
        assert status["done"] == 1
        assert status["totalTargets"] == 1

    async def test_delete_cleans_ocr_rows(self, client, monkeypatch):
        fake = FakeEngine(
            result=OcrResult(page_number=1, words=fake_words(["삭제", "전", "데이터", "확인"]))
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        assert (await client.delete(f"/api/documents/{doc['id']}")).status_code == 200
        async with get_session_factory()() as session:
            runs = (
                await session.execute(
                    select(func.count())
                    .select_from(OcrRun)
                    .where(OcrRun.document_id == uuid.UUID(doc["id"]))
                )
            ).scalar_one()
            blocks = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentBlock)
                    .where(DocumentBlock.document_id == uuid.UUID(doc["id"]))
                )
            ).scalar_one()
        assert runs == 0 and blocks == 0
