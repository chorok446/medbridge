"""합성 fixture → DB 씨딩. 실제 청크 파이프라인(rebuild_chunks)을 통과시킨다.

수치·부정·출처를 정확히 통제하려고 PDF 추출 대신 Document/Page/Block을 직접 만든다.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.enums import BlockType, PageExtractionStatus, ProcessingStatus, ScanVerdict
from app.models.extraction import DocumentBlock, DocumentPage
from app.qa_eval.manifest import Fixture
from app.services.search.chunking import rebuild_chunks


async def seed_document(session: AsyncSession, user_id: uuid.UUID, fixture: Fixture) -> uuid.UUID:
    """fixture로 문서·페이지·블록을 만들고 청크를 재계산한다. 문서 id 반환."""
    doc_id = uuid.uuid4()
    text_join = "\n".join(b for page in fixture.pages for b in page)
    sha = hashlib.sha256(f"{fixture.name}:{text_join}".encode()).hexdigest()
    session.add(Document(
        id=doc_id,
        user_id=user_id,
        title=f"eval::{fixture.name}",
        original_filename=f"{fixture.name}.pdf",
        sha256=sha,
        file_size=max(1, len(text_join.encode("utf-8"))),
        page_count=len(fixture.pages),
        language=fixture.language,
        processing_status=ProcessingStatus.EXTRACTED,
        content_revision=1,
    ))
    await session.flush()  # 문서를 먼저 넣어 페이지·블록 FK를 만족시킨다

    for page_index, blocks in enumerate(fixture.pages):
        page_id = uuid.uuid4()
        session.add(DocumentPage(
            id=page_id,
            document_id=doc_id,
            page_number=page_index + 1,
            width=612.0,
            height=792.0,
            raw_text="\n".join(blocks),
            normalized_text="\n".join(blocks),
            extraction_method="digital",
            extraction_status=PageExtractionStatus.EXTRACTED,
            scan_verdict=ScanVerdict.DIGITAL,
        ))
        await session.flush()  # 페이지를 먼저 넣어 블록 FK를 만족시킨다
        y = 72.0
        for block_index, block_text in enumerate(blocks):
            height = 18.0 + 14.0 * block_text.count("\n")
            session.add(DocumentBlock(
                id=uuid.uuid4(),
                document_id=doc_id,
                page_id=page_id,
                block_index=block_index,
                block_type=BlockType.TEXT,
                x0=72.0,
                y0=y,
                x1=540.0,
                y1=y + height,
                text=block_text,
                reading_order=block_index,
            ))
            y += height + 8.0

    await session.flush()
    await rebuild_chunks(session, doc_id)
    await session.commit()
    return doc_id
