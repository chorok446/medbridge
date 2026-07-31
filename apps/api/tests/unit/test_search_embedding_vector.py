"""임베딩 인터페이스·벡터 검색 단위 테스트."""

import uuid

import pytest

from app.services.search.embedding import (
    DeterministicEmbeddingProvider,
    DisabledEmbeddingProvider,
)
from app.services.search.vector import (
    VectorCandidate,
    VectorValidationError,
    cosine_similarity,
    pack_vector,
    unpack_vector,
)


class TestEmbeddingProviders:
    def test_disabled_provider_reports_unavailable(self):
        p = DisabledEmbeddingProvider()
        assert p.available is False
        with pytest.raises(RuntimeError):
            p.embed_query("hello")
        with pytest.raises(RuntimeError):
            p.embed_documents(["hello"])

    def test_deterministic_provider_is_reproducible(self):
        p = DeterministicEmbeddingProvider(dimension=16)
        assert p.available is True
        assert p.dimension == 16
        v1 = p.embed_query("심부전의 이해")
        v2 = p.embed_query("심부전의 이해")
        assert v1 == v2
        assert len(v1) == 16

    def test_deterministic_provider_differs_by_text(self):
        p = DeterministicEmbeddingProvider(dimension=16)
        assert p.embed_query("심부전") != p.embed_query("고혈압")

    def test_embed_documents_matches_query_shape(self):
        p = DeterministicEmbeddingProvider(dimension=8)
        docs = p.embed_documents(["문서 1", "문서 2"])
        assert len(docs) == 2
        assert all(len(v) == 8 for v in docs)


class TestVectorPacking:
    def test_pack_unpack_roundtrip(self):
        vec = [0.1, -0.2, 0.3, 0.0]
        restored = unpack_vector(pack_vector(vec))
        assert all(abs(a - b) < 1e-6 for a, b in zip(vec, restored, strict=True))

    def test_rejects_empty_vector(self):
        with pytest.raises(VectorValidationError):
            pack_vector([])

    def test_rejects_nan(self):
        with pytest.raises(VectorValidationError):
            pack_vector([1.0, float("nan"), 0.5])

    def test_rejects_inf(self):
        with pytest.raises(VectorValidationError):
            pack_vector([1.0, float("inf")])


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self):
        v = [1.0, 2.0, 3.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_score_negative_one(self):
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_dimension_mismatch_rejected(self):
        with pytest.raises(VectorValidationError):
            cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0])

    def test_zero_vector_scores_zero_not_error(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_ranking_orders_by_similarity(self):
        query = [1.0, 0.0, 0.0]
        candidates = [
            VectorCandidate(chunk_id=uuid.uuid4(), vector=[1.0, 0.0, 0.0]),  # 완전 일치
            VectorCandidate(chunk_id=uuid.uuid4(), vector=[0.0, 1.0, 0.0]),  # 직교
            VectorCandidate(chunk_id=uuid.uuid4(), vector=[0.9, 0.1, 0.0]),  # 근접
        ]
        ranked = sorted(candidates, key=lambda c: cosine_similarity(query, c.vector), reverse=True)
        assert ranked[0].vector == [1.0, 0.0, 0.0]
        assert ranked[1].vector == [0.9, 0.1, 0.0]
        assert ranked[2].vector == [0.0, 1.0, 0.0]
