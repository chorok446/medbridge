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

from app.models.extraction import DocumentBlock, DocumentPage
from app.models.search import DocumentChunk
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
    return parts


def _select_blocks(
    pages: list[DocumentPage], blocks: list[DocumentBlock]
) -> list[tuple[int, DocumentBlock]]:
    """페이지별로 머리말·꼬리말을 제외하고, 디지털 블록이 있으면 그 페이지의 OCR
    블록은 제외한다(디지털 우선). 반환은 (page_number, block) 튜플의 읽기 순서 목록.
    """
    page_number_by_id = {p.id: p.page_number for p in pages}
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
        has_digital = any(_block_source_method(b) == "digital" for b in page_blocks)
        for b in sorted(page_blocks, key=lambda b: b.reading_order):
            if has_digital and _block_source_method(b) == "ocr":
                continue
            if not (b.text or "").strip() and not b.is_table:
                continue
            selected.append((page_number, b))
    return selected


def build_chunk_drafts(
    pages: list[DocumentPage], blocks: list[DocumentBlock]
) -> list[ChunkDraft]:
    """document_id 범위의 페이지·블록으로부터 청크 초안 목록을 만든다."""
    ordered = _select_blocks(pages, blocks)

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
            flush()
            drafts.append(
                ChunkDraft(
                    section_title=current_section,
                    text_parts=[block_text],
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

    drafts = build_chunk_drafts(pages, blocks)

    # 원자적 교체: 기존 청크 + FTS 미러를 지우고 새로 넣는다 (재실행 누적 방지).
    await session.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
    )
    await session.execute(
        text("DELETE FROM document_chunks_fts WHERE document_id = :doc_id"),
        {"doc_id": str(document_id)},
    )

    rows: list[DocumentChunk] = []
    seen_hashes: set[str] = set()
    for index, draft in enumerate(drafts):
        if not draft.refs:
            continue  # 출처 없는 청크는 저장하지 않는다
        chunk_text = normalize_text("\n\n".join(draft.text_parts))
        if not chunk_text:
            continue
        chunk_hash = _content_hash(chunk_text)
        if chunk_hash in seen_hashes:
            continue  # 동일 content_hash 청크는 중복 생성하지 않는다
        seen_hashes.add(chunk_hash)
        rows.append(
            DocumentChunk(
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
        )

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

    return len(rows)
