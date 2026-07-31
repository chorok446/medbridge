"""벡터 저장·조회·코사인 유사도 — numpy 없이 순수 Python(개인용 앱 규모에 충분).

임베딩이 없는 청크, 모델/차원이 다른 벡터는 후보에서 제외한다.
NaN·빈 벡터·차원 불일치는 저장·조회 양쪽에서 거부한다.
"""

import math
import uuid
from array import array
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.search import DocumentChunk


class VectorValidationError(ValueError):
    pass


def pack_vector(vector: list[float]) -> bytes:
    if not vector:
        raise VectorValidationError("빈 벡터는 저장할 수 없습니다.")
    if any(math.isnan(v) or math.isinf(v) for v in vector):
        raise VectorValidationError("NaN/Inf가 포함된 벡터는 저장할 수 없습니다.")
    return array("f", vector).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    return list(array("f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise VectorValidationError(f"벡터 차원이 다릅니다: {len(a)} != {len(b)}")
    if not a or not b:
        raise VectorValidationError("빈 벡터는 비교할 수 없습니다.")
    if any(math.isnan(v) or math.isinf(v) for v in (*a, *b)):
        raise VectorValidationError("NaN/Inf가 포함된 벡터는 비교할 수 없습니다.")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass
class VectorCandidate:
    chunk_id: uuid.UUID
    vector: list[float]


class ChunkVectorStore(Protocol):
    """향후 다른 저장소(예: 전용 벡터 DB)로 교체할 수 있도록 분리된 조회 인터페이스."""

    async def get_candidates(
        self, document_id: uuid.UUID, *, model_name: str, dimension: int
    ) -> list[VectorCandidate]: ...


class SqliteChunkVectorStore:
    """document_chunks.embedding_blob에서 같은 모델·차원의 후보만 읽는다.

    단일 문서 범위로만 조회한다(스펙 요구사항) — 전체 청크를 스캔하지 않는다.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_candidates(
        self, document_id: uuid.UUID, *, model_name: str, dimension: int
    ) -> list[VectorCandidate]:
        rows = (
            await self._session.execute(
                select(DocumentChunk.id, DocumentChunk.embedding_blob).where(
                    DocumentChunk.document_id == document_id,
                    DocumentChunk.embedding_model == model_name,
                    DocumentChunk.embedding_dimension == dimension,
                    DocumentChunk.embedding_blob.is_not(None),
                )
            )
        ).all()
        candidates: list[VectorCandidate] = []
        for chunk_id, blob in rows:
            if blob is None:
                continue
            vector = unpack_vector(blob)
            if len(vector) != dimension:
                continue  # 손상되었거나 차원이 어긋난 데이터는 조용히 건너뛴다
            candidates.append(VectorCandidate(chunk_id=chunk_id, vector=vector))
        return candidates
