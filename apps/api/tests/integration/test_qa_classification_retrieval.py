"""분류 질문은 주제에 속한 표의 이어지는 조각을 제한된 범위에서 함께 찾는다."""

import pytest

from app.db.session import get_session_factory
from app.services.qa import context
from app.services.qa.settings import CONTEXT_MAX_CHARS, CONTEXT_MAX_CHUNKS
from tests.integration.test_qa_retrieval import (
    add_chunk,
    add_generation,
)
from tests.integration.test_qa_retrieval import document_id as document_id


@pytest.mark.parametrize("question", ["순환장치의 분류", "순환장치의 분류에 대해 설명해주세요."])
async def test_classification_keeps_table_tail_and_next_page_criteria(document_id, question):
    async with get_session_factory()() as db:
        await add_chunk(db, document_id, 100, "순환장치", title="순환장치")
        await add_chunk(db, document_id, 102, "정의와 분류", pages=(1, 40))
        for i in range(103, 108):
            await add_chunk(db, document_id, i, f"주제 설명 조각 {i}")
        await add_chunk(db, document_id, 108, "분류 표: A형은 첫 방식이다.")
        tail = await add_chunk(db, document_id, 109, "B형은 두 번째 방식이다.", pages=(1, 2))
        for i in range(110, 130):
            await add_chunk(db, document_id, i, f"짧은 표 조각 {i}", pages=(2,))
        scale = await add_chunk(db, document_id, 130, "기능 단계의 원문 기준", pages=(2,))
        criterion = await add_chunk(db, document_id, 142, "기능 분류의 마지막 기준", pages=(2,))
        await add_chunk(db, document_id, 143, "다른 페이지 분류", pages=(3,))
        await add_chunk(db, document_id, 144, "범위만 겹치는 분류", pages=(0, 40))
        await add_chunk(db, document_id, 200, "멀리 떨어진 분류", pages=(1,))
        hidden = await add_generation(db, document_id, active=False)
        await add_chunk(db, document_id, 109, "숨긴 분류", generation=hidden)
        result = await context.retrieve(db, document_id, question)
        for expected in (tail, scale, criterion):
            assert str(expected.id) in result.lookup
            assert result.lookup[str(expected.id)].text == expected.normalized_text
            assert result.lookup[str(expected.id)].content_hash == expected.content_hash
        body = "\n".join(c.text for c in result.chunks)
        assert not any(word in body for word in ("다른 페이지", "범위만", "멀리", "숨긴"))
        assert len(result.chunks) <= CONTEXT_MAX_CHUNKS
        assert sum(len(c.text) for c in result.chunks) <= CONTEXT_MAX_CHARS


async def test_classification_does_not_search_unrelated_topics(document_id):
    async with get_session_factory()() as db:
        await add_chunk(db, document_id, 100, "다른장치", title="다른장치")
        await add_chunk(db, document_id, 101, "분류에 대해 설명하는 내용")
        assert not (await context.retrieve(
            db, document_id, "없는장치의 분류에 대해 설명해주세요."
        )).chunks
