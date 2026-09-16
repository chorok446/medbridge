"""계층 요약 계획과 재사용 키 (순수 로직, DB·네트워크 없음).

레벨 0(map)은 청크 그룹을, 레벨 1 이상(reduce)은 하위 노드 요약을 입력으로 받는다.
어느 단계도 모델 context를 넘지 않도록 fan-in을 고정 상한으로 묶는다.

재사용 키(input_hash)는 "같은 입력 + 같은 실행 조건이면 같은 결과"를 뜻한다. 문서
revision·청크 해시·모델·provider 지문(endpoint/local-native/digest/생성 설정)·프롬프트·
스키마·학습자 수준·언어가 하나라도 다르면 다른 해시가 되어 이전 결과를 섞어 쓰지 않는다.
"""

import hashlib
from dataclasses import dataclass

from app.services.summary.settings import REDUCE_FAN_IN


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_context_key(
    *,
    provider_name: str,
    model_name: str,
    provider_fingerprint: str,
    prompt_version: str,
    schema_version: int,
    learner_level: str,
    language: str,
    source_revision: int,
    source_chunk_hash: str,
) -> str:
    """실행 조건 지문 — 이 값이 다르면 이전 노드를 재사용하지 않는다."""
    return _sha256(
        "\x1f".join(
            [
                provider_name,
                model_name,
                provider_fingerprint,
                prompt_version,
                str(schema_version),
                learner_level,
                language,
                str(source_revision),
                source_chunk_hash,
            ]
        )
    )


def map_node_input_hash(context_key: str, chunk_id_text_pairs: list[tuple[str, str]]) -> str:
    """레벨 0 노드 키 — 그룹에 담긴 (chunk_id, 텍스트) 순서까지 반영한다."""
    parts = [f"{cid}:{_sha256(text)}" for cid, text in chunk_id_text_pairs]
    return _sha256(f"{context_key}\x1fL0\x1f" + "\n".join(parts))


def reduce_node_input_hash(
    context_key: str, level: int, child_hashes: list[tuple[str, str]]
) -> str:
    """레벨 1+ 노드 키 — 자식의 (input_hash, 요약 텍스트) 순서까지 반영한다."""
    parts = [f"{h}:{_sha256(text)}" for h, text in child_hashes]
    return _sha256(f"{context_key}\x1fL{level}\x1f" + "\n".join(parts))


def fan_in_batches(items: list, fan_in: int = REDUCE_FAN_IN) -> list[list]:
    """순서를 유지한 채 fan_in개씩 묶는다."""
    if fan_in < 2:
        raise ValueError("fan_in은 2 이상이어야 합니다.")
    return [items[i : i + fan_in] for i in range(0, len(items), fan_in)]


def plan_level_sizes(base_count: int, fan_in: int = REDUCE_FAN_IN) -> list[int]:
    """레벨별 노드 수. 마지막 레벨 노드 수는 fan_in 이하이며 구조화 reduce의 입력이 된다.

    예) 711, fan_in=8 → [711, 89, 12, 2]
    """
    if base_count <= 0:
        return []
    if fan_in < 2:
        raise ValueError("fan_in은 2 이상이어야 합니다.")
    sizes = [base_count]
    count = base_count
    while count > fan_in:
        count = -(-count // fan_in)  # ceil
        sizes.append(count)
    return sizes


def plan_total_nodes(base_count: int, fan_in: int = REDUCE_FAN_IN) -> int:
    """전체 모델 호출 노드 수(구조화 reduce 1회는 별도)."""
    return sum(plan_level_sizes(base_count, fan_in))


@dataclass
class NodePlan:
    """한 노드의 실행 계획 — 실행기는 이 순서대로 처리하고 결과를 체크포인트로 저장한다."""

    level: int
    position: int
    input_hash: str
