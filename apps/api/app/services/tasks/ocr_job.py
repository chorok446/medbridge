"""OCR 백그라운드 작업 — 페이지 순차 처리(동시 1), job 토큰 CAS, 취소·복구."""

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.core.logging import correlation_id_var, get_logger
from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, OcrRunStatus
from app.models.extraction import DocumentPage
from app.models.ocr import OcrRun
from app.services.documents import storage
from app.services.ocr import service as ocr_service
from app.services.ocr.service import PENDING
from app.services.ocr.settings import QUALITY_DPI
from app.services.tasks.jobs import latest_job

logger = get_logger(__name__)


def _detail(exc: Exception, limit: int = 300) -> str:
    """예외 메시지를 한 줄로 줄여 로그에 남긴다(원문 길이 제한)."""
    return " ".join(str(exc).split())[:limit]


async def _job_is_current(session, document_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    latest = await latest_job(session, document_id, JobType.OCR_DOCUMENT)
    return (
        latest is not None
        and latest.id == job_id
        and latest.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    )


async def run_ocr_job(
    document_id: uuid.UUID,
    correlation_id: str,
    *,
    language: str = "kor+eng",
    quality: str = "standard",
) -> None:
    correlation_id_var.set(correlation_id)
    factory = get_session_factory()
    dpi = QUALITY_DPI.get(quality, QUALITY_DPI["standard"])

    async with factory() as session:
        doc = await session.get(Document, document_id)
        if doc is None or doc.deleted_at is not None or doc.storage_key is None:
            return
        job = await latest_job(session, document_id, JobType.OCR_DOCUMENT)
        if job is None:
            return
        job_id = job.id
        job.status = JobStatus.RUNNING
        job.attempt_count += 1
        job.started_at = datetime.now(UTC)
        await session.commit()
        pdf_path = str(storage.get_storage().resolve_path(doc.storage_key))

    eng = ocr_service.engine()
    # 엔진 상태를 잡마다 한 번 남긴다. 페이지가 전부 실패했을 때 "무엇이 없어서"인지를
    # 로그만으로 좁힐 수 있어야 한다(실기기에서 45페이지가 전부 같은 예외 이름으로만
    # 기록돼 원인 후보를 하나도 배제할 수 없었다).
    describe = getattr(eng, "describe", None)
    if callable(describe):
        logger.info("ocr_engine_ready", document_id=str(document_id), **describe())

    processed = failed = 0
    # 형식은 성공했지만 내용이 노이즈인 페이지. 실패와 따로 세되 잡을 성공으로
    # 마감하지는 않는다 — 조용히 SUCCEEDED로 끝나면 오류 리포트에 흔적이 남지 않고,
    # 사용자는 본문이 빠진 문서를 '다 읽었다'로 받는다.
    unreliable = 0
    # 읽을 글자가 없던 페이지. 노이즈(unreliable)와 **따로 센다** — 스캔 교재의 백지
    # 뒷면·장 구분·도판 전용 페이지는 정상적으로 0~2단어가 나오고, 그건 사용자가
    # 손댈 수 있는 실패가 아니다. 한 장이라도 섞였다고 문서 전체를 실패로 마감하면
    # 멀쩡히 읽힌 문서마다 'OCR 실패'가 뜬다.
    empty = 0
    # 본문이 실제로 달라진 페이지 수. 결과가 같은 재실행까지 revision을 올리면 이미
    # 만들어 둔 요약 노드 수백 개가 재사용 키에서 무효가 된다.
    changed = 0
    while True:
        async with factory() as session:
            if not await _job_is_current(session, document_id, job_id):
                logger.info("ocr_job_superseded_or_cancelled", document_id=str(document_id))
                return
            page = (
                (
                    await session.execute(
                        select(DocumentPage)
                        .where(
                            DocumentPage.document_id == document_id,
                            DocumentPage.ocr_status == PENDING,
                        )
                        .order_by(DocumentPage.page_number)
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if page is None:
                break
            page.ocr_status = OcrRunStatus.RUNNING.value
            run = OcrRun(
                document_id=document_id,
                page_id=page.id,
                engine="tesseract",
                engine_version=eng.version,
                language=language,
                render_dpi=dpi,
                preprocessing_json={"grayscale": True},
            )
            session.add(run)
            await session.commit()
            page_number = page.page_number

        try:
            result = await asyncio.to_thread(
                eng.recognize_page, pdf_path, page_number, language=language, dpi=dpi
            )
            async with factory() as session:
                if not await _job_is_current(session, document_id, job_id):
                    return
                page = (
                    (
                        await session.execute(
                            select(DocumentPage).where(
                                DocumentPage.document_id == document_id,
                                DocumentPage.page_number == page_number,
                            )
                        )
                    )
                    .scalars()
                    .one()
                )
                run_row = (
                    (
                        await session.execute(
                            select(OcrRun)
                            .where(OcrRun.page_id == page.id)
                            .order_by(OcrRun.created_at.desc())
                            .limit(1)
                        )
                    )
                    .scalars()
                    .one()
                )
                page_changed = await ocr_service.apply_ocr_result(session, page, result, run_row)
                if page_changed:
                    # 페이지 본문과 같은 트랜잭션에서 revision을 올린다. 문서 전체 OCR이
                    # 끝날 때까지 미루면, 앞쪽 페이지는 새 본문인데 revision은 예전 값인
                    # 창이 생겨 동시 청크 rebuild가 혼합 세대를 정상으로 활성화할 수 있다.
                    await session.execute(
                        update(Document)
                        .where(Document.id == document_id)
                        .values(content_revision=Document.content_revision + 1)
                    )
                await session.commit()
                processed += 1
                changed += int(page_changed)
                if run_row.status == OcrRunStatus.OCR_LOW_CONFIDENCE:
                    unreliable += 1
                elif run_row.status == OcrRunStatus.OCR_EMPTY:
                    empty += 1
                logger.info(
                    "ocr_page_done",
                    document_id=str(document_id),
                    page=page_number,
                    # 저장된 단어 수 — 디지털 본문과 겹쳐 버린 몫은 빠져 있다.
                    words=run_row.word_count,
                    # 엔진이 읽어낸 단어 수(중복 제거 전) — mean_confidence의 모수이기도
                    # 하다. 이게 없으면 words=0인 세 사건이 같은 모양으로 남는다: 잘 읽고
                    # 전부 디지털과 중복, 단어가 임계값 미만, 진짜 백지. 대응이 다 다르다.
                    raw_words=len(result.words),
                    # 신뢰도가 바닥이라 아예 버린 단어 수. 이 필터는 OcrResult를 만들기
                    # 전에 걸려서, 이게 없으면 버려진 몫이 mean_confidence에도
                    # result_status에도 raw_words에도 안 잡힌다 — 한 쪽의 85%가 노이즈로
                    # 빠져도 "신뢰도 0.75, 정상 완료" 한 줄로만 남는다.
                    dropped_words=result.low_quality_dropped,
                    mean_confidence=run_row.mean_confidence,
                    duration_ms=run_row.duration_ms,
                    # 판독 품질 판정 결과. 이게 없으면 신뢰도 0.35짜리 노이즈 페이지와
                    # 정상 페이지가 같은 "완료" 한 줄로만 남아 로그로 구분할 수 없다.
                    result_status=run_row.status.value,
                    # 같은 결과를 다시 쓴 재실행인지 — 재시도가 실제로 무언가 바꿨는지
                    # 판단하는 유일한 신호다(결정론적 엔진은 같은 입력에 같은 답을 낸다).
                    changed=page_changed,
                )
        except Exception as exc:
            failed += 1
            logger.warning(
                "ocr_page_failed",
                document_id=str(document_id),
                page=page_number,
                error=type(exc).__name__,
                # 타입 이름만으로는 원인을 좁힐 수 없다. RuntimeError 하나에 바이너리
                # 미탐지·언어 로드 실패·이미지 읽기 실패·렌더 실패가 전부 뭉쳐 있어,
                # 실기기 전량 실패에서 후보를 하나도 배제하지 못했다.
                # 메시지에는 문서 본문이 들어가지 않는다(엔진 오류 문구·경로뿐).
                error_detail=_detail(exc),
            )
            async with factory() as session:
                if not await _job_is_current(session, document_id, job_id):
                    return  # 취소·교체된 실행은 실패 기록도 남기지 않는다 (H4)
                page = (
                    (
                        await session.execute(
                            select(DocumentPage).where(
                                DocumentPage.document_id == document_id,
                                DocumentPage.page_number == page_number,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if page is not None:
                    page.ocr_status = OcrRunStatus.OCR_FAILED.value
                    failed_run = (
                        (
                            await session.execute(
                                select(OcrRun)
                                .where(OcrRun.page_id == page.id)
                                .order_by(OcrRun.created_at.desc())
                                .limit(1)
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if failed_run is not None:
                        failed_run.status = OcrRunStatus.OCR_FAILED
                        failed_run.error_code = type(exc).__name__[:50]
                        failed_run.completed_at = datetime.now(UTC)
                    await session.commit()

    async with factory() as session:
        if not await _job_is_current(session, document_id, job_id):
            return
        job = await session.get(DocumentJob, job_id)
        doc = await session.get(Document, document_id)
        if job is not None:
            # 실패로 마감하는 기준은 "어느 한 쪽이라도 나빴나"가 아니라 "쓸 수 있는
            # 본문이 하나도 안 나왔나"다.
            #
            # failed + unreliable을 그대로 세면 45쪽 중 44쪽이 멀쩡히 읽혀도 1쪽이
            # 흐리다는 이유로 잡이 FAILED가 된다. 재시도는 같은 원본을 다시 읽을
            # 뿐이라 흐린 스캔은 매번 같은 결과를 내고, 사용자는 손쓸 방법 없이
            # 'OCR 실패'를 보며 누를 때마다 오류 보고서에 실패가 한 건씩 더 쌓인다.
            # 저신뢰·백지는 엔진 실패가 아니라 결과이고, 페이지별 상태와
            # lowConfidencePages로 이미 화면에 전달된다.
            #
            # 엔진 오류(failed)는 다르다 — 설치·경로 문제라 사용자가 대응할 수 있고,
            # 한 쪽이라도 나면 알려야 한다.
            usable = processed - empty - unreliable
            nothing_usable = processed > 0 and usable <= 0
            incomplete = failed
            job.status = JobStatus.FAILED if incomplete or nothing_usable else JobStatus.SUCCEEDED
            if incomplete or nothing_usable:
                job.failure_code = "OCR_PARTIAL_FAILURE"
                # 같은 코드라도 대응이 다르다. "엔진이 죽었다"는 설치·경로 문제고,
                # "읽긴 읽었는데 노이즈다"는 원본 스캔 품질 문제다. "한 글자도 못 읽었다"는
                # 또 다르다 — 스캔이 아예 비었거나 인식 언어가 맞지 않는 경우다.
                job.failure_reason = (
                    "page_errors_and_unreliable"
                    if failed and unreliable
                    else (
                        "page_errors"
                        if failed
                        else ("unreliable_pages" if unreliable else "no_text_found")
                    )
                )
            job.completed_at = datetime.now(UTC)
        if doc is not None:
            await ocr_service.rollup_document_status(session, doc)
            # content_revision은 각 변경 페이지의 본문과 같은 트랜잭션에서 이미 올렸다.
            # 여기서 다시 올리면 실제 변경 횟수보다 revision이 하나 더 진행한다.
        await session.commit()
        logger.info(
            "ocr_job_done",
            document_id=str(document_id),
            processed=processed,
            failed=failed,
            # 형식만 성공한 페이지 수 — failed=0인데 본문이 비는 이유가 여기서만 보인다.
            unreliable=unreliable,
            # 읽을 글자가 없던 페이지 수. 노이즈와 구분해 남긴다 — 백지가 몇 장인지
            # 모르면 "왜 본문이 적은가"를 로그만으로 좁힐 수 없다.
            empty=empty,
            changed=changed,
        )

    # 청크를 자동으로 다시 만든다(요약은 자동 생성하지 않는다 — 모델 호출은 사용자가
    # 실행). 청크 재생성 실패는 OCR 완료 자체를 되돌리지 않는다.
    #
    # 조건은 "내용이 바뀌었나"가 아니라 "청크가 실제로 낡았거나 없나"다. changed > 0
    # 하나만 보면, 청크 잡이 앱 종료로 중단된 문서에서 OCR을 다시 돌려도 결정론적
    # 엔진이 같은 결과를 내 changed == 0이 되고 재생성이 아예 등록되지 않는다 —
    # 검색·질문 화면은 영영 준비되지 않은 문서로 남고, 화면이 시키는 '다시 실행'을
    # 눌러도 아무 일이 일어나지 않는다. `processed > 0`으로 되돌리는 것도 답이 아니다.
    # 그러면 같은 결과를 다시 쓴 재실행마다 revision이 올라 요약 노드 수백 개가
    # 재사용 키에서 무효가 된다.
    #
    # chunk_revision은 성공한 재생성에서만 content_revision과 같아지므로, 그 불일치가
    # 곧 "없거나 낡았다"는 뜻이다.
    async with factory() as session:
        doc = await session.get(Document, document_id)
        stale_chunks = doc is not None and doc.chunk_revision != doc.content_revision
    if changed > 0 or stale_chunks:
        async with factory() as session:
            doc = await session.get(Document, document_id)
            if doc is not None and doc.deleted_at is None:
                from app.services.search import service as search_service

                try:
                    await search_service.start_chunk_rebuild(session, doc, correlation_id)
                except Exception:
                    logger.warning("ocr_chunk_rebuild_enqueue_failed", document_id=str(document_id))


async def mark_ocr_job_crashed(document_id: uuid.UUID) -> None:
    """크래시 경계 — RUNNING으로 남은 OCR 잡을 실패로 확정해 영구 차단을 막는다 (H5)."""
    factory = get_session_factory()
    async with factory() as session:
        job = await latest_job(session, document_id, JobType.OCR_DOCUMENT)
        if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            job.status = JobStatus.FAILED
            job.failure_code = "OCR_CRASHED"
            job.completed_at = datetime.now(UTC)
            # 실행 기록도 함께 닫는다 — RUNNING으로 남으면 다음 실행의 "바뀜" 판정이
            # 항상 참이 되어 요약 재사용 키가 통째로 무효가 된다.
            await ocr_service._close_open_runs(session, document_id, OcrRunStatus.OCR_FAILED)
            await session.commit()
