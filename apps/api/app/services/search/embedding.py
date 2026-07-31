"""임베딩 공급자 인터페이스 — 모델 벤더에 종속되지 않는다.

실제 상용 공급자는 이 스프린트에 추가하지 않는다. API 키·특정 벤더를
하드코딩하지 않고, 공급자가 비활성 상태여도 앱이 정상 동작해야 한다.
"""

import hashlib
import math
from typing import Protocol

from app.core.config import get_settings


class EmbeddingProvider(Protocol):
    model_name: str
    dimension: int
    available: bool

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class DisabledEmbeddingProvider:
    """실제 공급자가 설정되지 않은 정상 상태 — 키워드 검색은 이 상태에서도 동작해야 한다."""

    model_name = "disabled"
    dimension = 0
    available = False

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("임베딩 공급자가 비활성 상태입니다. 호출 전 available을 확인하세요.")

    def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("임베딩 공급자가 비활성 상태입니다. 호출 전 available을 확인하세요.")


class DeterministicEmbeddingProvider:
    """테스트 전용 — 실제 의미 유사도는 없다. 같은 텍스트는 항상 같은 벡터를 낸다."""

    model_name = "deterministic-test-v1"
    available = True

    def __init__(self, dimension: int = 32) -> None:
        self.dimension = dimension

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [digest[i % len(digest)] / 255.0 for i in range(self.dimension)]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def get_embedding_provider() -> EmbeddingProvider:
    """테스트에서 monkeypatch 가능한 단일 주입 지점 (ocr_service.engine()과 동일한 관례)."""
    if get_settings().embedding_provider == "deterministic":
        return DeterministicEmbeddingProvider()
    return DisabledEmbeddingProvider()
