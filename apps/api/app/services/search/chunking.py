"""청크 생성 — reading_order 순으로 블록을 묶어 검색 가능한 청크를 만든다.

모든 청크는 하나 이상의 원문 source_ref(page, block, bbox, reading_order, source_method)를
가진다. 출처 없는 청크는 만들지 않는다. 재실행 시 문서의 기존 청크를 원자적으로 교체한다
(펼쳐진 채로 누적되지 않는다).
"""

import asyncio
import hashlib
import re
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, bindparam, delete, exists, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Exists

from app.db.session import get_session_factory
from app.models.document import Document, DocumentJob
from app.models.enums import JobStatus, JobType, OcrRunStatus, ProcessingStatus
from app.models.extraction import DocumentBlock, DocumentPage, DocumentTable
from app.models.search import (
    DocumentChunk,
    DocumentChunkGeneration,
)
from app.services.extraction.geometry import overlap_ratio
from app.services.extraction.normalize import normalize_text
from app.services.search.settings import (
    CHUNK_MAX_CHARS,
    CHUNK_MIN_CHARS,
    CHUNK_TARGET_CHARS,
    SECTION_TITLE_MAX_CHARS,
)

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+|(?<=[다요]\.)\s*\n|(?<=[다요]\.)\s+")
_PAGE_BATCH_SIZE = 16
_STAGING_BATCH_SIZE = 100
_ORPHAN_GENERATION_AGE = timedelta(minutes=5)


class LowConfidenceOnlyDocument(Exception):
    """읽어낸 내용이 전부 저신뢰라 청크로 쓸 수 있는 게 하나도 남지 않았다.

    "글자가 없는 문서"와 구분해야 하는 상태다. 전자는 사용자가 할 수 있는 게 없지만,
    이쪽은 스캔 품질을 올려 OCR을 다시 돌리면 풀린다 — 그 차이를 화면이 말할 수 있어야
    한다. 예외로 올리는 이유는 이 경우 기존 청크를 지워서도, 준비 완료로 마감해서도
    안 되기 때문이다.
    """

    def __init__(self, suppressed_blocks: int) -> None:
        super().__init__(f"저신뢰로 제외된 블록 {suppressed_blocks}개 외에 청크로 쓸 내용이 없다")
        self.suppressed_blocks = suppressed_blocks


class ChunkRevisionChanged(Exception):
    """계획 이후 원문 revision이 바뀌어 shadow generation 활성화를 거부했다."""

    def __init__(self, planned_revision: int, current_revision: int | None) -> None:
        super().__init__(f"청크 계획 revision이 변경됨: {planned_revision} -> {current_revision}")
        self.planned_revision = planned_revision
        self.current_revision = current_revision


class ChunkRebuildInProgress(Exception):
    """동일 문서의 다른 generation이 아직 작성 중이다."""


class ChunkSourceUpdating(Exception):
    """OCR이 원문을 페이지 단위로 갱신 중이라 일관된 청크 세대를 만들 수 없다."""


def _active_ocr_exists(document_id: uuid.UUID) -> Exists:
    """활성 OCR 잡 존재 조건 — 계획 전 검사와 전환 CAS가 같은 정의를 쓴다."""
    return exists().where(
        DocumentJob.document_id == document_id,
        DocumentJob.job_type == JobType.OCR_DOCUMENT,
        DocumentJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
    )


def _is_generation_lease_conflict(exc: IntegrityError) -> bool:
    """SQLite partial unique lease 위반만 동시 rebuild로 분류한다."""
    message = str(exc.orig).lower()
    return (
        "unique constraint failed" in message
        and "document_chunk_generations.document_id" in message
    )


@dataclass(frozen=True)
class ChunkRebuildResult:
    """만든 청크 수와, 저신뢰라 청크에서 뺀 블록 수.

    둘을 같이 돌려주는 이유: 청크 수만 보면 "16226개나 만들었으니 이 문서는 잘 준비됐다"로
    읽히는데, 같은 실행에서 저신뢰로 통째로 빠진 페이지가 있으면 요약·검색·답변은 그
    내용을 영영 못 본 채 완결된 것처럼 보인다. `suppressed_low_confidence`가 "왜 요약에
    이 내용이 없나"에 답할 수 있는 유일한 숫자인데, 지금까지는 청크가 **0개**가 됐을
    때만 예외로 드러나고 일부만 빠지는 흔한 경우엔 계산 직후 버려졌다.
    """

    chunk_count: int
    suppressed_low_confidence: int


@dataclass
class SourceRef:
    page_number: int
    block_id: uuid.UUID
    bbox: tuple[float, float, float, float]
    reading_order: int
    source_method: str

    def to_json(self) -> dict:
        return {
            "pageNumber": self.page_number,
            "blockId": str(self.block_id),
            "bbox": list(self.bbox),
            "readingOrder": self.reading_order,
            "sourceMethod": self.source_method,
        }


@dataclass
class ChunkDraft:
    section_title: str | None
    text_parts: list[str] = field(default_factory=list)
    refs: list[SourceRef] = field(default_factory=list)
    is_table: bool = False

    @property
    def page_start(self) -> int:
        return min(r.page_number for r in self.refs)

    @property
    def page_end(self) -> int:
        return max(r.page_number for r in self.refs)

    @property
    def char_count(self) -> int:
        return sum(len(t) for t in self.text_parts)


def _block_source_method(block: DocumentBlock) -> str:
    return "ocr" if (block.metadata_json or {}).get("source") == "ocr" else "digital"


def _looks_like_section_title(text: str) -> bool:
    """제목 후보 휴리스틱 — 폰트 크기 정보가 없어 완벽하지 않다("가능하면 보존").

    한 줄이고 짧고, 마침표류로 끝나지 않는 블록을 제목으로 본다.
    """
    stripped = text.strip()
    if not stripped or "\n" in stripped:
        return False
    if len(stripped) > SECTION_TITLE_MAX_CHARS:
        return False
    return not stripped.endswith((".", "다.", "요.", "?", "!", ","))


def _split_long_text(text: str, limit: int) -> list[str]:
    """문장 경계에서 나누고, 그래도 넘치면 그 경계에서 강제로 자른다."""
    if len(text) <= limit:
        return [text]
    sentences = [s for s in _SENTENCE_BOUNDARY.split(text) if s.strip()]
    if len(sentences) <= 1:
        return [text[i : i + limit] for i in range(0, len(text), limit)]
    parts: list[str] = []
    buf = ""
    for sentence in sentences:
        if buf and len(buf) + len(sentence) > limit:
            parts.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf:
        parts.append(buf)
    # 문장 하나가 그 자체로 limit을 넘으면(긴 나열형 문장 등) 그 부분만 강제로 자른다.
    final: list[str] = []
    for part in parts:
        if len(part) <= limit:
            final.append(part)
        else:
            final.extend(part[i : i + limit] for i in range(0, len(part), limit))
    return final


_TABLE_MATCH_MIN_OVERLAP = 0.5


def _table_markdown_for_block(
    block: DocumentBlock, tables_by_page: dict[uuid.UUID, list[DocumentTable]]
) -> str | None:
    """is_table 블록과 같은 위치의 DocumentTable을 bbox 겹침으로 찾는다(직접 FK가
    없다 — 추출 파이프라인이 둘을 별도 테이블로 만든다). 찾으면 표 구조를 보존한
    markdown_text를, 못 찾으면 None을 반환해 블록 원문으로 대체한다."""
    block_bbox = (block.x0, block.y0, block.x1, block.y1)
    best_ratio = 0.0
    best_table: DocumentTable | None = None
    for t in tables_by_page.get(block.page_id, []):
        ratio = overlap_ratio(block_bbox, (t.x0, t.y0, t.x1, t.y1))
        if ratio >= _TABLE_MATCH_MIN_OVERLAP and ratio > best_ratio:
            best_ratio, best_table = ratio, t
    if best_table is not None and best_table.markdown_text.strip():
        return best_table.markdown_text
    return None


def _has_digital_text(block: DocumentBlock) -> bool:
    """이 블록이 '이미 읽어낸 디지털 본문'인지.

    빈 IMAGE 블록은 디지털 본문이 아니라 **OCR이 필요했던 이유** 그 자체다. 내용을
    보지 않고 출처만 보면 스캔 페이지가 항상 "디지털 블록이 있는 페이지"로 판정돼
    그 페이지의 OCR 텍스트가 통째로 청크에서 빠지고, 요약·검색이 스캔 문서의 본문을
    한 글자도 보지 못한다.
    """
    return _block_source_method(block) == "digital" and (
        bool((block.text or "").strip()) or block.is_table
    )


@dataclass
class _Selection:
    """청크에 넣을 블록과, 저신뢰 때문에 뺀 블록 수.

    `suppressed_low_confidence`가 필요한 이유: 결과가 0개일 때 "문서에 글자가 없다"와
    "글자는 있는데 우리가 못 믿어서 뺐다"를 구분해야 한다. 두 경우의 사용자 대응이
    완전히 다른데, 개수만 보면 똑같이 0이다.
    """

    blocks: list[tuple[int, DocumentBlock]]
    suppressed_low_confidence: int


def _select_blocks(pages: list[DocumentPage], blocks: list[DocumentBlock]) -> _Selection:
    """페이지별로 머리말·꼬리말을 제외하고, 디지털 본문이 있으면 그 페이지의 OCR
    블록은 제외한다(디지털 우선). 반환은 (page_number, block) 튜플의 읽기 순서 목록.

    판독 신뢰도가 바닥인 페이지의 OCR 블록도 제외한다. 형식은 성공이지만 내용은
    노이즈라, 그대로 두면 요약·검색이 그 노이즈를 근거로 삼고 사용자는 그것을 완결된
    결과로 신뢰한다(실기기: 재시도한 11페이지가 평균 신뢰도 0.32~0.38로 돌아왔다).
    """
    page_number_by_id = {p.id: p.page_number for p in pages}
    unreliable_page_ids = {
        p.id for p in pages if p.ocr_status == OcrRunStatus.OCR_LOW_CONFIDENCE.value
    }
    by_page: dict[int, list[DocumentBlock]] = {}
    for b in blocks:
        if b.is_header or b.is_footer:
            continue
        page_number = page_number_by_id.get(b.page_id)
        if page_number is None:
            continue
        by_page.setdefault(page_number, []).append(b)

    selected: list[tuple[int, DocumentBlock]] = []
    suppressed = 0
    for page_number, page_blocks in sorted(by_page.items()):
        has_digital = any(_has_digital_text(b) for b in page_blocks)
        for b in sorted(page_blocks, key=lambda b: b.reading_order):
            if _block_source_method(b) == "ocr" and (
                has_digital or b.page_id in unreliable_page_ids
            ):
                # 디지털 본문에 밀린 것은 대체재가 있으니 세지 않는다. 저신뢰로 뺀
                # 것만 센다 — 그것만이 "내용이 있었는데 못 쓴" 경우다.
                if not has_digital and (b.text or "").strip():
                    suppressed += 1
                continue
            if not (b.text or "").strip() and not b.is_table:
                continue
            selected.append((page_number, b))
    return _Selection(blocks=selected, suppressed_low_confidence=suppressed)


@dataclass
class ChunkPlan:
    """청크 초안과, 저신뢰로 빠진 블록 수. 0개의 이유를 구분하려면 둘 다 필요하다."""

    drafts: list[ChunkDraft]
    suppressed_low_confidence: int


class _ChunkDraftStream:
    """페이지 batch 사이에서 섹션·미완성 청크·짧은 병합 후보만 유지한다.

    한 문서 전체의 ``DocumentBlock``/``ChunkDraft`` 목록을 만들지 않으면서 기존
    ``build_chunk_drafts``와 같은 순서·병합 규칙을 보존한다. 메모리에 남는 것은 현재
    목표 크기 청크와 바로 이전 병합 후보뿐이다.
    """

    def __init__(self) -> None:
        self.current: ChunkDraft | None = None
        self.current_section: str | None = None
        self.merge_candidate: ChunkDraft | None = None
        self.suppressed_low_confidence = 0

    def _accept(self, draft: ChunkDraft, completed: list[ChunkDraft]) -> None:
        previous = self.merge_candidate
        if (
            previous is not None
            and not draft.is_table
            and not previous.is_table
            and previous.section_title == draft.section_title
            and previous.char_count < CHUNK_MIN_CHARS
            and draft.char_count < CHUNK_MIN_CHARS
        ):
            previous.text_parts.extend(draft.text_parts)
            previous.refs.extend(draft.refs)
            return
        if previous is not None:
            completed.append(previous)
        self.merge_candidate = draft

    def _flush_current(self, completed: list[ChunkDraft]) -> None:
        if self.current is not None and self.current.refs:
            self._accept(self.current, completed)
        self.current = None

    def consume(
        self,
        pages: Sequence[DocumentPage],
        blocks: Sequence[DocumentBlock],
        tables: Sequence[DocumentTable] | None = None,
    ) -> list[ChunkDraft]:
        """한 page batch를 소비하고 완전히 닫힌 draft만 반환한다."""
        selection = _select_blocks(list(pages), list(blocks))
        self.suppressed_low_confidence += selection.suppressed_low_confidence
        tables_by_page: dict[uuid.UUID, list[DocumentTable]] = {}
        for table in tables or ():
            tables_by_page.setdefault(table.page_id, []).append(table)

        completed: list[ChunkDraft] = []
        for page_number, block in selection.blocks:
            block_text = (block.text or "").strip()
            ref = SourceRef(
                page_number=page_number,
                block_id=block.id,
                bbox=(block.x0, block.y0, block.x1, block.y1),
                reading_order=block.reading_order,
                source_method=_block_source_method(block),
            )

            if block.is_table:
                self._flush_current(completed)
                table_text = _table_markdown_for_block(block, tables_by_page) or block_text
                self._accept(
                    ChunkDraft(
                        section_title=self.current_section,
                        text_parts=[table_text],
                        refs=[ref],
                        is_table=True,
                    ),
                    completed,
                )
                continue

            if _looks_like_section_title(block_text):
                self._flush_current(completed)
                self.current_section = block_text
                self.current = ChunkDraft(
                    section_title=self.current_section,
                    text_parts=[block_text],
                    refs=[ref],
                )
                continue

            if self.current is None:
                self.current = ChunkDraft(
                    section_title=self.current_section,
                    text_parts=[],
                    refs=[],
                )

            if len(block_text) > CHUNK_MAX_CHARS:
                self._flush_current(completed)
                for part in _split_long_text(block_text, CHUNK_MAX_CHARS):
                    self._accept(
                        ChunkDraft(
                            section_title=self.current_section,
                            text_parts=[part],
                            refs=[ref],
                        ),
                        completed,
                    )
                continue

            if self.current.char_count + len(block_text) > CHUNK_TARGET_CHARS and self.current.refs:
                self._flush_current(completed)
                self.current = ChunkDraft(
                    section_title=self.current_section,
                    text_parts=[],
                    refs=[],
                )

            self.current.text_parts.append(block_text)
            self.current.refs.append(ref)

        return completed

    def finish(self) -> list[ChunkDraft]:
        completed: list[ChunkDraft] = []
        self._flush_current(completed)
        if self.merge_candidate is not None:
            completed.append(self.merge_candidate)
            self.merge_candidate = None
        return completed


def build_chunk_drafts(
    pages: list[DocumentPage],
    blocks: list[DocumentBlock],
    tables: list[DocumentTable] | None = None,
) -> ChunkPlan:
    """document_id 범위의 페이지·블록으로부터 청크 초안 목록을 만든다."""
    stream = _ChunkDraftStream()
    drafts = [*stream.consume(pages, blocks, tables), *stream.finish()]
    return ChunkPlan(
        drafts=drafts,
        suppressed_low_confidence=stream.suppressed_low_confidence,
    )


def _merge_short_adjacent(drafts: list[ChunkDraft]) -> list[ChunkDraft]:
    """같은 섹션의 인접 청크가 둘 다 짧으면 합친다. 표 청크는 절대 병합하지 않는다."""
    merged: list[ChunkDraft] = []
    for draft in drafts:
        if (
            merged
            and not draft.is_table
            and not merged[-1].is_table
            and merged[-1].section_title == draft.section_title
            and merged[-1].char_count < CHUNK_MIN_CHARS
            and draft.char_count < CHUNK_MIN_CHARS
        ):
            merged[-1].text_parts.extend(draft.text_parts)
            merged[-1].refs.extend(draft.refs)
            continue
        merged.append(draft)
    return merged


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class _PreparedChunk:
    id: uuid.UUID
    chunk_index: int
    section_title: str | None
    normalized_text: str
    token_count: int
    page_start: int
    page_end: int
    source_refs_json: list[dict]
    content_hash: str


def _prepare_chunk(index: int, draft: ChunkDraft) -> _PreparedChunk | None:
    if not draft.refs:
        return None
    chunk_text = normalize_text("\n\n".join(draft.text_parts))
    if not chunk_text:
        return None
    return _PreparedChunk(
        id=uuid.uuid4(),
        chunk_index=index,
        section_title=draft.section_title,
        normalized_text=chunk_text,
        token_count=max(1, round(len(chunk_text) / 4)),
        page_start=draft.page_start,
        page_end=draft.page_end,
        source_refs_json=[ref.to_json() for ref in draft.refs],
        content_hash=_content_hash(chunk_text),
    )


async def _load_page_batch(
    session: AsyncSession,
    document_id: uuid.UUID,
    cursor: tuple[int, uuid.UUID] | None,
) -> list[DocumentPage]:
    conditions = [DocumentPage.document_id == document_id]
    if cursor is not None:
        page_number, page_id = cursor
        conditions.append(
            or_(
                DocumentPage.page_number > page_number,
                and_(
                    DocumentPage.page_number == page_number,
                    DocumentPage.id > page_id,
                ),
            )
        )
    return list(
        (
            await session.execute(
                select(DocumentPage)
                .where(*conditions)
                .order_by(DocumentPage.page_number, DocumentPage.id)
                .limit(_PAGE_BATCH_SIZE)
            )
        ).scalars()
    )


async def _load_page_content(
    session: AsyncSession, pages: Sequence[DocumentPage]
) -> tuple[list[DocumentBlock], list[DocumentTable]]:
    page_ids = [page.id for page in pages]
    if not page_ids:
        return [], []
    blocks = list(
        (
            await session.execute(
                select(DocumentBlock)
                .where(DocumentBlock.page_id.in_(page_ids))
                .order_by(DocumentBlock.page_id, DocumentBlock.reading_order)
            )
        ).scalars()
    )
    tables = list(
        (
            await session.execute(select(DocumentTable).where(DocumentTable.page_id.in_(page_ids)))
        ).scalars()
    )
    return blocks, tables


def _merge_prepared(first: _PreparedChunk, duplicate: _PreparedChunk) -> None:
    first.source_refs_json = [*first.source_refs_json, *duplicate.source_refs_json]
    first.page_start = min(first.page_start, duplicate.page_start)
    first.page_end = max(first.page_end, duplicate.page_end)


async def _write_staging_batch(
    session: AsyncSession,
    generation: DocumentChunkGeneration,
    chunks: Sequence[_PreparedChunk],
) -> int:
    """최대 ``_STAGING_BATCH_SIZE``개를 쓰고 즉시 commit해 writer lock을 놓는다."""
    by_hash: dict[str, _PreparedChunk] = {}
    for chunk in chunks:
        duplicate = by_hash.get(chunk.content_hash)
        if duplicate is None:
            by_hash[chunk.content_hash] = chunk
        else:
            _merge_prepared(duplicate, chunk)

    hashes = list(by_hash)
    existing_rows = {
        row.content_hash: row
        for row in (
            await session.execute(
                select(DocumentChunk)
                .where(
                    DocumentChunk.document_id == generation.document_id,
                    DocumentChunk.generation_id == generation.id,
                    DocumentChunk.content_hash.in_(hashes),
                )
                .execution_options(include_inactive_chunks=True)
            )
        ).scalars()
    }
    new_rows: list[DocumentChunk] = []
    for chunk_hash, chunk in by_hash.items():
        existing = existing_rows.get(chunk_hash)
        if existing is not None:
            existing.source_refs_json = [
                *existing.source_refs_json,
                *chunk.source_refs_json,
            ]
            existing.page_start = min(existing.page_start, chunk.page_start)
            existing.page_end = max(existing.page_end, chunk.page_end)
            continue
        new_rows.append(
            DocumentChunk(
                id=chunk.id,
                document_id=generation.document_id,
                generation_id=generation.id,
                chunk_index=chunk.chunk_index,
                section_title=chunk.section_title,
                normalized_text=chunk.normalized_text,
                token_count=chunk.token_count,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                source_refs_json=chunk.source_refs_json,
                content_hash=chunk.content_hash,
            )
        )

    session.add_all(new_rows)
    await session.flush()
    # 살아 있는 대형 rebuild는 page/write batch마다 lease를 갱신한다. 프로세스가 죽으면
    # 이 시각이 멈춰 다음 rebuild가 안전하게 orphan으로 판단할 수 있다.
    await session.execute(
        update(DocumentChunkGeneration)
        .where(DocumentChunkGeneration.id == generation.id)
        .values(updated_at=datetime.now(UTC))
    )
    if new_rows:
        await session.execute(
            text(
                "INSERT INTO document_chunks_fts"
                "(chunk_id, document_id, normalized_text, section_title) "
                "VALUES (:chunk_id, :document_id, :normalized_text, :section_title)"
            ),
            [
                {
                    "chunk_id": str(row.id),
                    "document_id": generation.shadow_document_id,
                    "normalized_text": row.normalized_text,
                    "section_title": row.section_title or "",
                }
                for row in new_rows
            ],
        )
    await session.commit()
    return len(new_rows)


async def _delete_generation_rows(
    session: AsyncSession,
    document_id: uuid.UUID,
    generation_id: uuid.UUID | None,
) -> None:
    """비활성 generation 청크를 bounded transaction으로 정리한다."""
    generation_filter = (
        DocumentChunk.generation_id.is_(None)
        if generation_id is None
        else DocumentChunk.generation_id == generation_id
    )
    while True:
        ids = list(
            (
                await session.execute(
                    select(DocumentChunk.id)
                    .where(
                        DocumentChunk.document_id == document_id,
                        generation_filter,
                    )
                    .limit(_STAGING_BATCH_SIZE)
                    .execution_options(include_inactive_chunks=True)
                )
            ).scalars()
        )
        if not ids:
            break
        await session.execute(delete(DocumentChunk).where(DocumentChunk.id.in_(ids)))
        await session.commit()


async def _delete_shadow_fts_batches(session: AsyncSession, shadow_document_id: str) -> None:
    while True:
        rowids = list(
            (
                await session.execute(
                    text(
                        "SELECT rowid FROM document_chunks_fts "
                        "WHERE document_id = :document_id LIMIT :limit"
                    ),
                    {
                        "document_id": shadow_document_id,
                        "limit": _STAGING_BATCH_SIZE,
                    },
                )
            ).scalars()
        )
        if not rowids:
            break
        await session.execute(
            text("DELETE FROM document_chunks_fts WHERE rowid IN :rowids").bindparams(
                bindparam("rowids", expanding=True)
            ),
            {"rowids": rowids},
        )
        await session.commit()


async def _discard_generation(generation_id: uuid.UUID) -> None:
    """별도 session에서 자기 shadow만 batch 정리한다."""
    async with get_session_factory()() as cleanup_session:
        generation = await cleanup_session.get(DocumentChunkGeneration, generation_id)
        if generation is None:
            return
        active_generation_id = (
            await cleanup_session.execute(
                select(Document.active_chunk_generation_id).where(
                    Document.id == generation.document_id
                )
            )
        ).scalar_one_or_none()
        # 전환 commit 직후 task가 취소돼 caller가 성공 여부를 못 받은 경우에도 이미
        # 활성화된 generation은 절대 보상 삭제하지 않는다.
        if active_generation_id == generation_id:
            return
        await _delete_generation_rows(cleanup_session, generation.document_id, generation.id)
        await _delete_shadow_fts_batches(cleanup_session, generation.shadow_document_id)
        await cleanup_session.delete(generation)
        await cleanup_session.commit()


async def _cleanup_inactive_generations(
    document_id: uuid.UUID,
    active_generation_id: uuid.UUID | None,
) -> None:
    """process kill로 남은 orphan과 직전 활성 generation을 다음 실행에 회수한다."""
    async with get_session_factory()() as cleanup_session:
        orphan_cutoff = datetime.now(UTC) - _ORPHAN_GENERATION_AGE
        generations = list(
            (
                await cleanup_session.execute(
                    select(DocumentChunkGeneration)
                    .outerjoin(
                        DocumentJob,
                        DocumentJob.id == DocumentChunkGeneration.owner_job_id,
                    )
                    .where(
                        DocumentChunkGeneration.document_id == document_id,
                        DocumentChunkGeneration.id != active_generation_id,
                        or_(
                            DocumentChunkGeneration.status != "building",
                            and_(
                                DocumentChunkGeneration.owner_job_id.is_not(None),
                                or_(
                                    DocumentJob.id.is_(None),
                                    DocumentJob.status.notin_(
                                        [JobStatus.QUEUED, JobStatus.RUNNING]
                                    ),
                                ),
                            ),
                            and_(
                                DocumentChunkGeneration.owner_job_id.is_(None),
                                DocumentChunkGeneration.updated_at < orphan_cutoff,
                            ),
                        ),
                    )
                )
            ).scalars()
        )
        for generation in generations:
            await _delete_generation_rows(cleanup_session, document_id, generation.id)
            await _delete_shadow_fts_batches(cleanup_session, generation.shadow_document_id)
            await cleanup_session.delete(generation)
            await cleanup_session.commit()

        if active_generation_id is not None:
            # 첫 generation 전환 뒤 남은 0014 이전 legacy(NULL) 청크도 bounded 정리한다.
            await _delete_generation_rows(cleanup_session, document_id, None)
            await _delete_shadow_fts_batches(cleanup_session, str(document_id))


async def _activate_generation(
    session: AsyncSession,
    generation_id: uuid.UUID,
    document_id: uuid.UUID,
    planned_revision: int,
    expected_generation_id: uuid.UUID | None,
) -> None:
    """revision CAS 뒤 shadow를 활성 테이블로 전환한다.

    첫 write가 ``content_revision`` 조건부 UPDATE라 SQLite writer lock을 얻은 뒤 계획
    revision을 확정한다. 이후 어느 문장이라도 실패·취소되면 트랜잭션 전체가 rollback돼
    기존 청크와 FTS가 그대로 남는다.
    """
    await session.rollback()  # 마지막 page SELECT가 연 read transaction을 닫는다.
    async with session.begin():
        generation = await session.get(DocumentChunkGeneration, generation_id)
        if generation is None or generation.source_revision != planned_revision:
            raise ChunkRevisionChanged(planned_revision, None)

        generation_guard = (
            Document.active_chunk_generation_id.is_(None)
            if expected_generation_id is None
            else Document.active_chunk_generation_id == expected_generation_id
        )
        guarded_document_id = (
            await session.execute(
                update(Document)
                .where(
                    Document.id == document_id,
                    Document.deleted_at.is_(None),
                    Document.processing_status.notin_(
                        [ProcessingStatus.DELETING, ProcessingStatus.DELETED]
                    ),
                    Document.content_revision == planned_revision,
                    ~_active_ocr_exists(document_id),
                    generation_guard,
                )
                .values(
                    chunk_revision=planned_revision,
                    active_chunk_generation_id=generation_id,
                )
                .returning(Document.id)
            )
        ).scalar_one_or_none()
        if guarded_document_id is None:
            if await session.scalar(select(_active_ocr_exists(document_id))):
                raise ChunkSourceUpdating(
                    f"OCR 처리 중에는 청크 세대를 전환할 수 없습니다: {document_id}"
                )
            current_revision = (
                await session.execute(
                    select(Document.content_revision).where(Document.id == document_id)
                )
            ).scalar_one_or_none()
            raise ChunkRevisionChanged(planned_revision, current_revision)
        generation.status = "active"


async def rebuild_chunks(
    session: AsyncSession,
    document_id: uuid.UUID,
    *,
    owner_job_id: uuid.UUID | None = None,
) -> ChunkRebuildResult:
    """bounded shadow generation을 만들고 revision-guarded 전환으로 활성화한다."""
    generation_id: uuid.UUID | None = None
    try:
        if await session.scalar(select(_active_ocr_exists(document_id))):
            raise ChunkSourceUpdating(
                f"OCR 처리 중에는 청크를 재생성할 수 없습니다: {document_id}"
            )
        document_state = (
            await session.execute(
                select(
                    Document.content_revision,
                    Document.active_chunk_generation_id,
                ).where(
                    Document.id == document_id,
                    Document.deleted_at.is_(None),
                    Document.processing_status.notin_(
                        [ProcessingStatus.DELETING, ProcessingStatus.DELETED]
                    ),
                )
            )
        ).one_or_none()
        if document_state is None:
            raise ValueError(f"문서를 찾을 수 없습니다: {document_id}")
        planned_revision, starting_generation_id = document_state

        # 호출자가 문서·페이지를 flush만 한 뒤 바로 재생성을 호출하는 경로도 있다.
        # bounded staging은 어차피 내부 commit을 사용하므로, 계획 revision과 그 원문 행을
        # 먼저 확정한다. 여기서 rollback하면 parent document까지 사라져 generation FK가
        # 실패하고, 더 나쁘게는 caller의 추출 결과를 조용히 잃는다.
        await session.commit()
        # 이전 강제 종료에서 남은 비활성 shadow를 먼저 회수한다. 현재 활성 세트는 보존한다.
        await _cleanup_inactive_generations(document_id, starting_generation_id)

        generation_id = uuid.uuid4()
        generation = DocumentChunkGeneration(
            id=generation_id,
            document_id=document_id,
            source_revision=planned_revision,
            shadow_document_id=f"shadow:{generation_id}",
            owner_job_id=owner_job_id,
        )
        session.add(generation)
        # 계획 revision을 먼저 영속화한다. 이후 모든 staging commit이 이 generation에
        # 귀속되고, 활성 청크에는 아직 아무 변화도 없다.
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if _is_generation_lease_conflict(exc):
                raise ChunkRebuildInProgress(
                    f"이미 청크를 재생성 중입니다: {document_id}"
                ) from exc
            raise

        stream = _ChunkDraftStream()
        buffer: list[_PreparedChunk] = []
        draft_index = 0
        chunk_count = 0
        cursor: tuple[int, uuid.UUID] | None = None

        while pages := await _load_page_batch(session, document_id, cursor):
            blocks, tables = await _load_page_content(session, pages)
            for draft in stream.consume(pages, blocks, tables):
                prepared = _prepare_chunk(draft_index, draft)
                draft_index += 1
                if prepared is not None:
                    buffer.append(prepared)
                if len(buffer) >= _STAGING_BATCH_SIZE:
                    chunk_count += await _write_staging_batch(session, generation, buffer)
                    buffer.clear()
            last_page = pages[-1]
            cursor = (last_page.page_number, last_page.id)
            # expire_on_commit=False여도 batch 객체를 문서 전체 동안 붙잡지 않는다.
            del blocks, tables, pages
            session.expunge_all()
            await session.rollback()  # page batch의 read transaction도 문서 전체 동안 잡지 않는다.

        for draft in stream.finish():
            prepared = _prepare_chunk(draft_index, draft)
            draft_index += 1
            if prepared is not None:
                buffer.append(prepared)
            if len(buffer) >= _STAGING_BATCH_SIZE:
                chunk_count += await _write_staging_batch(session, generation, buffer)
                buffer.clear()
        if buffer:
            chunk_count += await _write_staging_batch(session, generation, buffer)
            buffer.clear()

        if not chunk_count and stream.suppressed_low_confidence:
            raise LowConfidenceOnlyDocument(stream.suppressed_low_confidence)

        await _activate_generation(
            session,
            generation_id,
            document_id,
            planned_revision,
            starting_generation_id,
        )
        # 전환 commit 뒤에만 이전 세트를 batch 정리한다. 정리 실패는 다음 rebuild에서
        # 다시 회수할 수 있고 새 활성 generation의 정확성에는 영향을 주지 않는다.
        with suppress(Exception):
            await _cleanup_inactive_generations(document_id, generation_id)
        return ChunkRebuildResult(
            chunk_count=chunk_count,
            suppressed_low_confidence=stream.suppressed_low_confidence,
        )
    except BaseException:
        # CancelledError도 포함한다. 전환 트랜잭션을 먼저 rollback하고, task 취소와
        # 독립적으로 자기 shadow generation만 지운다. 활성 generation은 건드리지 않는다.
        with suppress(BaseException):
            await asyncio.shield(session.rollback())
        if generation_id is not None:
            with suppress(BaseException):
                await asyncio.shield(_discard_generation(generation_id))
        raise
