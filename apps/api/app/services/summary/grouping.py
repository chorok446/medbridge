"""청크 → 그룹 묶기 (map 단계 입력).

reading order(청크 입력 순서)를 유지한 bounded char packing. 섹션 제목은 **선호하는
경계**일 뿐 강제 분할점이 아니다 — 그룹이 GROUP_MIN_CHARS를 넘긴 뒤에만 제목 변경을
경계로 쓴다. 짧은 제목이 잦은 문서에서 제목마다 끊으면 map 호출 수가 폭증한다
(실측: 4,265,890자·7,965청크 → 3,074그룹, packing 적용 시 약 711그룹).

각 그룹은 원본 chunk id를 그대로 들고 다닌다(출처 연결 유지).
"""

from dataclasses import dataclass, field

from app.services.summary.provider import ChunkInput
from app.services.summary.settings import GROUP_MAX_CHARS, GROUP_MIN_CHARS


@dataclass
class ChunkGroup:
    group_id: str
    section_title: str | None
    chunks: list[ChunkInput] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(c.text) for c in self.chunks)


def build_groups(chunks: list[ChunkInput]) -> list[ChunkGroup]:
    """입력 순서(=reading order)를 유지하며 길이 기준으로 그룹을 만든다.

    - 길이 상한(GROUP_MAX_CHARS)을 넘기면 반드시 새 그룹으로 나눈다.
    - 섹션 제목이 바뀌어도 현재 그룹이 GROUP_MIN_CHARS 미만이면 계속 채운다(작은 섹션 병합).
    - 단일 청크가 상한보다 길면 그 청크만 담긴 그룹이 된다(청크를 쪼개지 않는다).

    group.section_title은 그룹의 **첫 청크** 제목이다. 여러 섹션이 병합된 그룹에서는
    대표 제목으로만 쓰이며, 출처는 제목이 아니라 chunk id로 유지된다.
    """
    groups: list[ChunkGroup] = []
    current: ChunkGroup | None = None

    for chunk in chunks:
        if not chunk.text.strip():
            continue
        if current is not None:
            exceeds_limit = current.char_count + len(chunk.text) > GROUP_MAX_CHARS
            section_boundary = (
                chunk.section_title != current.section_title
                and current.char_count >= GROUP_MIN_CHARS
            )
            if exceeds_limit or section_boundary:
                current = None
        if current is None:
            current = ChunkGroup(group_id=f"g{len(groups)}", section_title=chunk.section_title)
            groups.append(current)
        current.chunks.append(chunk)

    return [g for g in groups if g.chunks]
