"""청크 → 그룹 묶기 (map 단계 입력).

section_title 우선, reading order(청크 입력 순서) 유지, 최대 입력 길이 제한, 너무 큰
섹션은 여러 그룹으로 분할. 각 그룹은 원본 chunk id를 그대로 들고 다닌다(출처 연결 유지).
"""

from dataclasses import dataclass, field

from app.services.summary.provider import ChunkInput
from app.services.summary.settings import GROUP_MAX_CHARS


@dataclass
class ChunkGroup:
    group_id: str
    section_title: str | None
    chunks: list[ChunkInput] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(c.text) for c in self.chunks)


def build_groups(chunks: list[ChunkInput]) -> list[ChunkGroup]:
    """입력 순서(=reading order)를 유지하며 섹션·길이 기준으로 그룹을 만든다."""
    groups: list[ChunkGroup] = []
    current: ChunkGroup | None = None

    def start_new(section_title: str | None) -> ChunkGroup:
        g = ChunkGroup(group_id=f"g{len(groups)}", section_title=section_title)
        groups.append(g)
        return g

    for chunk in chunks:
        if not chunk.text.strip():
            continue
        section = chunk.section_title
        needs_new = (
            current is None
            or current.section_title != section  # 섹션이 바뀌면 새 그룹
            or current.char_count + len(chunk.text) > GROUP_MAX_CHARS  # 길이 초과 분할
        )
        if needs_new:
            current = start_new(section)
        assert current is not None
        current.chunks.append(chunk)

    return [g for g in groups if g.chunks]
