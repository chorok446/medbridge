"""OCR 통합 테스트 — 실제 Tesseract(가능 시) + FakeEngine(제어 경로)."""

import uuid

import pytest
from sqlalchemy import func, select
from structlog.testing import capture_logs

from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, OcrRunStatus
from app.models.extraction import DocumentBlock, DocumentPage
from app.models.ocr import OcrRun
from app.models.search import DocumentChunk
from app.services.extraction.ocr import OcrResult, OcrWord
from app.services.ocr import service as ocr_service
from app.services.ocr.settings import OCR_HIGH_QUALITY_DPI, OCR_LOW_MEMORY_DPI
from app.services.search.chunking import LowConfidenceOnlyDocument, rebuild_chunks
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
        self.dpis: list[int] = []

    @property
    def available(self) -> bool:
        return True

    @property
    def version(self) -> str:
        return "fake-1.0"

    def recognize_page(self, pdf_path, page_number, *, language="kor+eng", dpi=0):
        self.calls.append(page_number)
        self.dpis.append(dpi)
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
            page = (
                await session.execute(
                    select(DocumentPage).where(
                        DocumentPage.document_id == uuid.UUID(doc["id"]),
                        DocumentPage.page_number == 1,
                    )
                )
            ).scalars().one()
        # OCR이 실제로 단어를 읽어냈다 — 디지털 기준선 위로 늘어난 만큼이 OCR 몫이다.
        assert page.word_count > page.digital_word_count

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
            before = (
                await session.execute(
                    select(DocumentPage).where(
                        DocumentPage.document_id == uuid.UUID(doc["id"]),
                        DocumentPage.page_number == 1,
                    )
                )
            ).scalars().one()
            digital_before = before.word_count

        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        status = (await client.get(f"/api/documents/{doc['id']}/ocr-status")).json()["data"]
        assert status["remainingOcrPages"] == []
        # 디지털 페이지 1은 OCR 대상이 아니었고 단어 수 불변
        async with get_session_factory()() as session:
            page1 = (
                await session.execute(
                    select(DocumentPage).where(
                        DocumentPage.document_id == uuid.UUID(doc["id"]),
                        DocumentPage.page_number == 1,
                    )
                )
            ).scalar_one()
        assert page1.word_count == digital_before, "OCR이 디지털 페이지를 건드렸다"
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
            # OCR 본문은 블록에 담긴다 — 교체 여부는 거기서 본다(단어 테이블은 없앴다).
            blocks = (
                await session.execute(
                    select(DocumentBlock)
                    .join(DocumentPage, DocumentPage.id == DocumentBlock.page_id)
                    .where(DocumentPage.document_id == uuid.UUID(doc["id"]))
                )
            ).scalars().all()
            texts = " ".join(
                b.text or "" for b in blocks if (b.metadata_json or {}).get("source") == "ocr"
            )
            runs = (
                await session.execute(
                    select(func.count())
                    .select_from(OcrRun)
                    .where(OcrRun.document_id == uuid.UUID(doc["id"]))
                )
            ).scalar_one()
        assert "두번째" in texts and "첫번째" not in texts  # 교체, 누적 아님
        # 최종 실행분이 두 번 저장되는 회귀(부분 삭제 실패·블록 재추가)도 잡는다 —
        # 포함 여부만 보면 같은 실행분의 중복은 통과해 버린다.
        assert texts.count("두번째") == 1
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


class TestOcrPageLogIsDiagnosable:
    """`words=0`이 세 가지 다른 사건을 같은 모양으로 남기지 않게 한다.

    실기기 로그에 `words 0 / conf 0.9 / ocr_completed`(잘 읽었지만 전부 디지털과 중복),
    `words 0 / conf 0.9639 / ocr_empty`(단어가 임계값 미만), `words 0 / conf 0.0 /
    ocr_empty`(진짜 백지)가 거의 구분되지 않는 모양으로 섞여 있었다. 대응이 각각 다르다.
    """

    async def test_raw_word_count_survives_digital_dedupe(self, client, monkeypatch):
        """디지털 본문과 통째로 겹쳐 저장된 단어가 0개여도 읽어낸 양은 로그에 남는다."""
        doc = await upload_extracted(client, fx.scanned_page_with_short_caption())

        # 이 쪽의 디지털 블록과 정확히 같은 자리에 OCR 단어를 놓는다 — 중복 제거가
        # 전부 걷어내므로 저장되는 단어는 0개가 된다.
        async with get_session_factory()() as session:
            digital = [
                b
                for b in (
                    await session.execute(
                        select(DocumentBlock).where(
                            DocumentBlock.document_id == uuid.UUID(doc["id"])
                        )
                    )
                ).scalars().all()
                if (b.metadata_json or {}).get("source") != "ocr" and (b.text or "").strip()
            ]
        assert digital, "겹칠 디지털 블록이 없으면 이 테스트는 공허하다"
        box = digital[0]
        overlapping = [
            OcrWord(
                bbox=(box.x0, box.y0, box.x1, box.y1),
                text=t,
                confidence=0.9,
                block_index=1,
                paragraph_index=1,
                line_index=1,
                word_index=i + 1,
            )
            for i, t in enumerate(["그림", "1.", "심장"])
        ]
        fake = FakeEngine(
            result=OcrResult(page_number=1, words=overlapping, mean_confidence=0.9)
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)

        with capture_logs() as logs:
            await client.post(f"/api/documents/{doc['id']}/ocr")
            await drain_jobs()

        done = [e for e in logs if e.get("event") == "ocr_page_done"]
        assert done, [e.get("event") for e in logs]
        entry = done[0]
        # 저장된 단어는 0개다 — 여기까지는 기존 로그와 같다.
        assert entry["words"] == 0
        # 하지만 엔진은 3단어를 읽어냈다. 이 값이 없으면 백지와 구분할 수 없다.
        assert entry["raw_words"] == 3

    async def test_blank_page_reports_zero_raw_words(self, client, monkeypatch):
        """진짜 백지는 raw_words도 0이다 — 위 경우와 로그에서 갈린다."""
        monkeypatch.setattr(ocr_service, "engine", lambda: FakeEngine())
        doc = await upload_extracted(client, fx.blank_image_page())

        with capture_logs() as logs:
            await client.post(f"/api/documents/{doc['id']}/ocr")
            await drain_jobs()

        done = [e for e in logs if e.get("event") == "ocr_page_done"]
        assert done, [e.get("event") for e in logs]
        assert done[0]["raw_words"] == 0
        assert done[0]["result_status"] == OcrRunStatus.OCR_EMPTY.value


def _low_confidence_result(texts: list[str]) -> OcrResult:
    """판독에 실패한 것과 다름없는 결과 — 단어는 있지만 신뢰도가 바닥이다.

    실기기 재시도에서 11페이지가 평균 신뢰도 0.32~0.38로 돌아왔다. 형식은 성공이라
    아무 게이트에도 걸리지 않았지만 내용은 노이즈였다.
    """
    return OcrResult(
        page_number=1, words=fake_words(texts, conf=0.35), mean_confidence=0.35
    )


class TestLowConfidenceIsolation:
    """저신뢰 OCR 결과를 '읽은 내용'으로 취급하지 않는다.

    형식만 성공한 노이즈가 청크로 들어가면 요약·검색이 그 노이즈를 근거로 삼고,
    사용자는 그것을 완결된 결과로 신뢰한다.
    """

    async def test_low_confidence_text_is_kept_out_of_chunks(self, client, monkeypatch):
        fake = FakeEngine(result=_low_confidence_result(["ㄱㅂㅅ", "ㅁㄴㅇ", "ㄹㅇㅋ", "ㅍㅌㅊ"]))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        doc_id = uuid.UUID(doc["id"])
        async with get_session_factory()() as session:
            # 남길 내용이 하나도 없으면 조용히 0개로 끝내지 않고 이유를 들고 멈춘다.
            with pytest.raises(LowConfidenceOnlyDocument):
                await rebuild_chunks(session, doc_id)

        async with get_session_factory()() as session:
            texts = [
                r[0]
                for r in (
                    await session.execute(
                        select(DocumentChunk.normalized_text).where(
                            DocumentChunk.document_id == doc_id
                        )
                    )
                ).all()
            ]
        assert not any("ㄱㅂㅅ" in t for t in texts), texts

    async def test_partial_suppression_is_reported_next_to_the_chunk_count(
        self, client, monkeypatch
    ):
        """일부만 저신뢰인 문서 — 청크는 만들어지지만 그만큼이 빠져 있다.

        전부 저신뢰일 때만 예외로 알리고 이 흔한 경우엔 아무 말도 하지 않으면, 청크 수는
        멀쩡해 보이는데 요약·검색은 그 페이지를 영영 못 본다. 실기기 오류 보고서에
        `chunk_count=16226`만 남아 "요약에 이 내용이 왜 없나"를 좁힐 단서가 없었다.
        """
        fake = FakeEngine(result=_low_confidence_result(["ㄱㅂㅅ", "ㅁㄴㅇ", "ㄹㅇㅋ", "ㅍㅌㅊ"]))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.mixed_digital_and_scanned())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        async with get_session_factory()() as session:
            result = await rebuild_chunks(session, uuid.UUID(doc["id"]))
            await session.commit()

        # 디지털 1쪽이 살아 있으므로 청크는 만들어진다 — 그래서 개수만으로는 멀쩡해 보인다.
        assert result.chunk_count > 0
        assert result.suppressed_low_confidence > 0

    async def test_low_confidence_keeps_document_unresolved(self, client, monkeypatch):
        """ocr_empty와 같은 취급 — '다 읽었다'로 승격하지 않는다."""
        fake = FakeEngine(result=_low_confidence_result(["흐림", "판독", "불가", "상태"]))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        after = (await client.get(f"/api/documents/{doc['id']}")).json()["data"]
        assert after["processingStatus"] == "ocr_required"

    async def test_low_confidence_marks_the_job_partially_failed(
        self, client, monkeypatch
    ):
        """잡이 조용히 성공으로 끝나면 오류 리포트에 아무 흔적도 남지 않는다."""
        fake = FakeEngine(result=_low_confidence_result(["흐림", "판독", "불가", "상태"]))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        async with get_session_factory()() as session:
            job = (
                await session.execute(
                    select(DocumentJob)
                    .where(
                        DocumentJob.document_id == uuid.UUID(doc["id"]),
                        DocumentJob.job_type == JobType.OCR_DOCUMENT,
                    )
                    .order_by(DocumentJob.created_at.desc())
                    .limit(1)
                )
            ).scalars().one()
        assert job.failure_code == "OCR_PARTIAL_FAILURE"
        # 같은 실패 코드라도 "엔진이 죽었다"와 "읽긴 읽었는데 노이즈다"는 대응이 다르다.
        assert job.failure_reason == "unreliable_pages"

    async def test_all_low_confidence_does_not_report_chunking_success(
        self, client, monkeypatch
    ):
        """전 페이지가 저신뢰면 청크가 0개가 되는데, 그걸 '준비 완료'로 마감하면 안 된다.

        마감해 버리면 문서는 영구히 쓸 수 없는 상태가 되고 화면은 '문서 검색 준비하기'만
        반복해서 권한다 — 눌러도 같은 코드가 같은 0개를 만든다.
        """
        fake = FakeEngine(result=_low_confidence_result(["ㄱㅂㅅ", "ㅁㄴㅇ", "ㄹㅇㅋ", "ㅍㅌㅊ"]))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        assert (
            await client.post(f"/api/documents/{doc['id']}/chunks/rebuild")
        ).status_code == 202
        await drain_jobs()

        async with get_session_factory()() as session:
            job = (
                await session.execute(
                    select(DocumentJob)
                    .where(
                        DocumentJob.document_id == uuid.UUID(doc["id"]),
                        DocumentJob.job_type == JobType.CHUNK_REBUILD,
                    )
                    .order_by(DocumentJob.created_at.desc())
                    .limit(1)
                )
            ).scalars().one()
            document = await session.get(Document, uuid.UUID(doc["id"]))

        assert job.status == JobStatus.FAILED, "0개인데 성공으로 끝났다"
        assert job.failure_code == "CHUNK_LOW_CONFIDENCE_ONLY"
        # 준비 완료 도장을 찍으면 안 된다 — 찍히면 stale 판정도 못 하게 된다.
        assert document.chunk_revision != document.content_revision

    async def test_status_surfaces_the_reason(self, client, monkeypatch):
        """화면이 '왜 안 되는지' 말할 수 있어야 한다 — 코드가 안 나가면 안내를 못 바꾼다."""
        fake = FakeEngine(result=_low_confidence_result(["ㄱㅂㅅ", "ㅁㄴㅇ", "ㄹㅇㅋ", "ㅍㅌㅊ"]))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        await client.post(f"/api/documents/{doc['id']}/chunks/rebuild")
        await drain_jobs()

        status = (
            await client.get(f"/api/documents/{doc['id']}/chunks/status")
        ).json()["data"]
        assert status["chunkCount"] == 0
        assert status["jobStatus"] == "failed"
        assert status["failureCode"] == "CHUNK_LOW_CONFIDENCE_ONLY"

    async def test_empty_rebuild_does_not_destroy_existing_chunks(
        self, client, monkeypatch
    ):
        """새 결과가 빌 것을 알기 전에 기존 청크를 지우면 안 된다.

        이전에 쓸 수 있던 문서가 OCR 재시도 한 번으로 통째로 비어 버린다.
        """
        fake = FakeEngine(result=_low_confidence_result(["ㄱㅂㅅ", "ㅁㄴㅇ", "ㄹㅇㅋ", "ㅍㅌㅊ"]))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        doc_id = uuid.UUID(doc["id"])
        async with get_session_factory()() as session:
            session.add(
                DocumentChunk(
                    id=uuid.uuid4(),
                    document_id=doc_id,
                    chunk_index=0,
                    section_title=None,
                    normalized_text="이전 실행에서 만들어 둔 쓸 수 있는 청크",
                    token_count=10,
                    page_start=1,
                    page_end=1,
                    source_refs_json=[],
                    content_hash="deadbeef",
                )
            )
            await session.commit()

        async with get_session_factory()() as session:
            with pytest.raises(LowConfidenceOnlyDocument):
                await rebuild_chunks(session, doc_id)

        async with get_session_factory()() as session:
            survived = (
                await session.execute(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.document_id == doc_id)
                )
            ).scalar_one()
        assert survived == 1, "빈 결과를 만들면서 기존 청크를 지웠다"

    async def test_blank_page_does_not_fail_the_whole_document(
        self, client, monkeypatch
    ):
        """백지·도판 페이지는 정상적으로 0~2단어가 나온다 — 그게 실패는 아니다.

        스캔 교재에는 백지 뒷면과 장 구분 페이지가 반드시 섞여 있다. 그 한 장 때문에
        나머지가 전부 정상 인식된 문서에도 'OCR 실패'가 뜨고 오류 보고서에 실패
        기록이 남으면, 사용자는 손댈 것이 없는 실패를 계속 보게 된다.
        """

        class MixedEngine(FakeEngine):
            """첫 페이지는 정상, 나머지는 백지."""

            def recognize_page(self, pdf_path, page_number, *, language="kor+eng", dpi=0):
                self.calls.append(page_number)
                if page_number == 1:
                    return OcrResult(
                        page_number=page_number,
                        words=fake_words(["정상", "인식", "결과", "확인"], conf=0.95),
                        mean_confidence=0.95,
                    )
                return OcrResult(page_number=page_number)  # 단어 0개 = OCR_EMPTY

        monkeypatch.setattr(ocr_service, "engine", lambda: MixedEngine())
        doc = await upload_extracted(client, fx.blank_image_page(pages=3))
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        async with get_session_factory()() as session:
            job = (
                await session.execute(
                    select(DocumentJob)
                    .where(
                        DocumentJob.document_id == uuid.UUID(doc["id"]),
                        DocumentJob.job_type == JobType.OCR_DOCUMENT,
                    )
                    .order_by(DocumentJob.created_at.desc())
                    .limit(1)
                )
            ).scalars().one()
        assert job.status == JobStatus.SUCCEEDED, (
            f"백지 한 장으로 문서 전체가 실패했다: {job.failure_reason}"
        )

    async def test_document_with_nothing_readable_is_still_a_failure(
        self, client, monkeypatch
    ):
        """한 글자도 못 읽었으면 그건 보고해야 할 실패다."""
        monkeypatch.setattr(ocr_service, "engine", lambda: FakeEngine())
        doc = await upload_extracted(client, fx.blank_image_page(pages=2))
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        async with get_session_factory()() as session:
            job = (
                await session.execute(
                    select(DocumentJob)
                    .where(
                        DocumentJob.document_id == uuid.UUID(doc["id"]),
                        DocumentJob.job_type == JobType.OCR_DOCUMENT,
                    )
                    .order_by(DocumentJob.created_at.desc())
                    .limit(1)
                )
            ).scalars().one()
        assert job.status == JobStatus.FAILED
        assert job.failure_reason == "no_text_found"

    async def test_engine_errors_and_noise_get_different_reasons(
        self, client, monkeypatch
    ):
        """엔진 실패는 저신뢰와 구분돼야 한다 — 보고서에서 원인을 좁히는 유일한 단서다."""
        fake = FakeEngine(error=RuntimeError("boom"))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        async with get_session_factory()() as session:
            job = (
                await session.execute(
                    select(DocumentJob)
                    .where(
                        DocumentJob.document_id == uuid.UUID(doc["id"]),
                        DocumentJob.job_type == JobType.OCR_DOCUMENT,
                    )
                    .order_by(DocumentJob.created_at.desc())
                    .limit(1)
                )
            ).scalars().one()
        assert job.failure_code == "OCR_PARTIAL_FAILURE"
        assert job.failure_reason == "page_errors"

    async def test_good_pages_still_reach_the_chunks(self, client, monkeypatch):
        """저신뢰만 걸러낸다 — 정상 판독 결과까지 버리면 안 된다."""
        fake = FakeEngine(
            result=OcrResult(
                page_number=1,
                words=fake_words(["정상", "판독", "결과", "보존"], conf=0.95),
                mean_confidence=0.95,
            )
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        doc_id = uuid.UUID(doc["id"])
        async with get_session_factory()() as session:
            await rebuild_chunks(session, doc_id)
            await session.commit()
            texts = [
                r[0]
                for r in (
                    await session.execute(
                        select(DocumentChunk.normalized_text).where(
                            DocumentChunk.document_id == doc_id
                        )
                    )
                ).all()
            ]
        assert any("정상" in t for t in texts), texts


class TestRetryActuallyRetries:
    async def test_retry_uses_high_quality_even_with_an_empty_body(
        self, client, monkeypatch
    ):
        """'다시 읽기'는 첫 실행보다 높은 해상도로 다시 읽어야 한다.

        프런트엔드는 빈 `{}`를 보낸다. 그걸 '사용자가 standard를 골랐다'로 읽으면
        재시도가 첫 실행과 같은 DPI로 돌고, tesseract는 결정론적이라 바이트 단위로
        같은 결과가 나온다 — 재시도 버튼이 아무것도 하지 않는다.
        """
        fake = FakeEngine(result=_low_confidence_result(["흐림", "판독", "불가", "상태"]))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        first_dpi = fake.dpis[-1]

        res = await client.post(f"/api/documents/{doc['id']}/ocr/retry", json={})
        assert res.status_code == 200, res.text
        await drain_jobs()

        assert fake.dpis[-1] == OCR_HIGH_QUALITY_DPI
        assert fake.dpis[-1] > first_dpi

    async def test_explicit_quality_in_the_body_is_still_honoured(
        self, client, monkeypatch
    ):
        """사용자가 품질을 명시하면 그 값을 쓴다 — 기본값이 선택을 덮어쓰지 않는다."""
        fake = FakeEngine(result=_low_confidence_result(["흐림", "판독", "불가", "상태"]))
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()

        res = await client.post(
            f"/api/documents/{doc['id']}/ocr/retry", json={"quality": "low_memory"}
        )
        assert res.status_code == 200, res.text
        await drain_jobs()
        assert fake.dpis[-1] == OCR_LOW_MEMORY_DPI


class TestContentRevisionOnlyMovesOnRealChange:
    async def test_identical_rerun_does_not_invalidate_downstream_work(
        self, client, monkeypatch
    ):
        """결과가 같으면 content_revision을 올리지 않는다.

        revision은 요약 노드 재사용 키(build_context_key)에 들어간다. 아무것도 바뀌지
        않은 재실행에 revision을 올리면, 실기기에서처럼 이미 성공한 map 노드 수백 개가
        통째로 무효가 되고 사용자는 같은 요약을 처음부터 다시 기다린다.
        """
        fake = FakeEngine(
            result=OcrResult(
                page_number=1,
                words=fake_words(["동일", "결과", "재실행", "검증"], conf=0.95),
                mean_confidence=0.95,
            )
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        doc_id = uuid.UUID(doc["id"])
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        async with get_session_factory()() as session:
            first = (await session.get(Document, doc_id)).content_revision

        await client.post(f"/api/documents/{doc['id']}/ocr", json={"pages": [1]})
        await drain_jobs()

        async with get_session_factory()() as session:
            assert (await session.get(Document, doc_id)).content_revision == first

    async def test_changed_text_still_bumps_the_revision(self, client, monkeypatch):
        """내용이 실제로 바뀌면 revision은 올라가야 한다 — 그래야 stale 요약이 걸린다."""
        fake = FakeEngine(
            result=OcrResult(
                page_number=1,
                words=fake_words(["첫번째", "판독", "결과", "확인"], conf=0.95),
                mean_confidence=0.95,
            )
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake)
        doc = await upload_extracted(client, fx.blank_image_page())
        doc_id = uuid.UUID(doc["id"])
        await client.post(f"/api/documents/{doc['id']}/ocr")
        await drain_jobs()
        async with get_session_factory()() as session:
            first = (await session.get(Document, doc_id)).content_revision

        fake2 = FakeEngine(
            result=OcrResult(
                page_number=1,
                words=fake_words(["두번째", "판독", "결과", "교체"], conf=0.95),
                mean_confidence=0.95,
            )
        )
        monkeypatch.setattr(ocr_service, "engine", lambda: fake2)
        await client.post(f"/api/documents/{doc['id']}/ocr", json={"pages": [1]})
        await drain_jobs()

        async with get_session_factory()() as session:
            assert (await session.get(Document, doc_id)).content_revision > first
