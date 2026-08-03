"""청크 생성 — reading_order 순으로 블록을 묶어 검색 가능한 청크를 만든다.

모든 청크는 하나 이상의 원문 source_ref(page, block, bbox, reading_order, source_method)를
가진다. 출처 없는 청크는 만들지 않는다. 재실행 시 문서의 기존 청크를 원자적으로 교체한다
(펼쳐진 채로 누적되지 않는다).
"""

import hashlib
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.enums import OcrRunStatus
from app.models.extraction import DocumentBlock, DocumentPage, DocumentTable
from app.models.search import DocumentChunk
from app.services.extraction.geometry import overlap_ratio
from app.services.extraction.normalize import normalize_text
from app.services.search.settings import (
    CHUNK_MAX_CHARS,
    CHUNK_MIN_CHARS,
    CHUNK_TARGET_CHARS,
    SECTION_TITLE_MAX_CHARS,
)

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+|(?<=[다요]\.)\s*\n|(?<=[다요]\.)\s+")


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


def _select_blocks(
    pages: list[DocumentPage], blocks: list[DocumentBlock]
) -> list[tuple[int, DocumentBlock]]:
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
    for page_number, page_blocks in sorted(by_page.items()):
        has_digital = any(_has_digital_text(b) for b in page_blocks)
        for b in sorted(page_blocks, key=lambda b: b.reading_order):
            if _block_source_method(b) == "ocr" and (
                has_digital or b.page_id in unreliable_page_ids
            ):
                continue
            if not (b.text or "").strip() and not b.is_table:
                continue
            selected.append((page_number, b))
    return selected


def build_chunk_drafts(
    pages: list[DocumentPage],
    blocks: list[DocumentBlock],
    tables: list[DocumentTable] | None = None,
) -> list[ChunkDraft]:
    """document_id 범위의 페이지·블록으로부터 청크 초안 목록을 만든다."""
    ordered = _select_blocks(pages, blocks)
    tables_by_page: dict[uuid.UUID, list[DocumentTable]] = {}
    for t in tables or []:
        tables_by_page.setdefault(t.page_id, []).append(t)

    drafts: list[ChunkDraft] = []
    current: ChunkDraft | None = None
    current_section: str | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and current.refs:
            drafts.append(current)
        current = None

    for page_number, block in ordered:
        block_text = (block.text or "").strip()
        ref = SourceRef(
            page_number=page_number,
            block_id=block.id,
            bbox=(block.x0, block.y0, block.x1, block.y1),
            reading_order=block.reading_order,
            source_method=_block_source_method(block),
        )

        if block.is_table:
            # 표는 절대 본문과 평탄화하지 않는다 — 있던 청크를 닫고 표 전용 청크를 낸다.
            # 가능하면 행·열 구조가 살아있는 DocumentTable.markdown_text를 쓴다
            # (block.text는 PDF 추출 순서로 흩어진 셀 텍스트라 구조를 잃는다).
            flush()
            table_text = _table_markdown_for_block(block, tables_by_page) or block_text
            drafts.append(
                ChunkDraft(
                    section_title=current_section,
                    text_parts=[table_text],
                    refs=[ref],
                    is_table=True,
                )
            )
            continue

        if _looks_like_section_title(block_text):
            # 새 섹션 제목 — 지금까지 쌓인 청크를 닫고 제목을 다음 섹션 이름으로 삼는다.
            flush()
            current_section = block_text
            current = ChunkDraft(section_title=current_section, text_parts=[block_text], refs=[ref])
            continue

        if current is None:
            current = ChunkDraft(section_title=current_section, text_parts=[], refs=[])

        if len(block_text) > CHUNK_MAX_CHARS:
            # 단일 블록이 지나치게 길면 그 블록만 문장 경계로 나눠 각각 청크로 낸다.
            flush()
            for part in _split_long_text(block_text, CHUNK_MAX_CHARS):
                drafts.append(
                    ChunkDraft(section_title=current_section, text_parts=[part], refs=[ref])
                )
            current = None
            continue

        if current.char_count + len(block_text) > CHUNK_TARGET_CHARS and current.refs:
            flush()
            current = ChunkDraft(section_title=current_section, text_parts=[], refs=[])

        current.text_parts.append(block_text)
        current.refs.append(ref)

    flush()
    return _merge_short_adjacent(drafts)


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


async def rebuild_chunks(session: AsyncSession, document_id: uuid.UUID) -> int:
    """문서 청크를 원자적으로 재계산·교체한다. 반환: 생성된 청크 수."""
    pages = list(
        (
            await session.execute(
                select(DocumentPage).where(DocumentPage.document_id == document_id)
            )
        ).scalars()
    )
    blocks = list(
        (
            await session.execute(
                select(DocumentBlock).where(DocumentBlock.document_id == document_id)
            )
        ).scalars()
    )
    tables = list(
        (
            await session.execute(
                select(DocumentTable).where(
                    DocumentTable.page_id.in_([p.id for p in pages])
                )
            )
        ).scalars()
    )

    drafts = build_chunk_drafts(pages, blocks, tables)

    # 원자적 교체: 기존 청크 + FTS 미러를 지우고 새로 넣는다 (재실행 누적 방지).
    await session.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
    )
    await session.execute(
        text("DELETE FROM document_chunks_fts WHERE document_id = :doc_id"),
        {"doc_id": str(document_id)},
    )

    rows: list[DocumentChunk] = []
    rows_by_hash: dict[str, DocumentChunk] = {}
    for index, draft in enumerate(drafts):
        if not draft.refs:
            continue  # 출처 없는 청크는 저장하지 않는다
        chunk_text = normalize_text("\n\n".join(draft.text_parts))
        if not chunk_text:
            continue
        chunk_hash = _content_hash(chunk_text)
        existing = rows_by_hash.get(chunk_hash)
        if existing is not None:
            # 동일 content_hash는 새 청크를 만들지 않되, 다른 위치에 나온 출처는
            # 잃지 않도록 기존 청크에 병합한다(반복되는 문구가 여러 페이지에 있는 경우).
            existing.source_refs_json = [
                *existing.source_refs_json,
                *[r.to_json() for r in draft.refs],
            ]
            existing.page_start = min(existing.page_start, draft.page_start)
            existing.page_end = max(existing.page_end, draft.page_end)
            continue
        row = DocumentChunk(
            id=uuid.uuid4(),
            document_id=document_id,
            chunk_index=index,
            section_title=draft.section_title,
            normalized_text=chunk_text,
            token_count=max(1, round(len(chunk_text) / 4)),
            page_start=draft.page_start,
            page_end=draft.page_end,
            source_refs_json=[r.to_json() for r in draft.refs],
            content_hash=chunk_hash,
        )
        rows.append(row)
        rows_by_hash[chunk_hash] = row

    session.add_all(rows)
    await session.flush()

    for row in rows:
        await session.execute(
            text(
                "INSERT INTO document_chunks_fts"
                "(chunk_id, document_id, normalized_text, section_title) "
                "VALUES (:chunk_id, :document_id, :normalized_text, :section_title)"
            ),
            {
                "chunk_id": str(row.id),
                "document_id": str(document_id),
                "normalized_text": row.normalized_text,
                "section_title": row.section_title or "",
            },
        )

    # 청크 세트가 만들어진 시점의 content_revision을 기록한다 — 이후 문서가 바뀌면
    # chunk_revision != content_revision이 되어 청크가 stale로 판정된다.
    doc = await session.get(Document, document_id)
    if doc is not None:
        doc.chunk_revision = doc.content_revision

    return len(rows)
