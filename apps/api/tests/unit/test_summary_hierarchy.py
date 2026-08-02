"""계층 요약 계획·grouping 단위 테스트 (순수 로직, DB·모델 없음)."""

import pytest

from app.services.summary.grouping import build_groups
from app.services.summary.hierarchy import (
    build_context_key,
    fan_in_batches,
    map_node_input_hash,
    plan_level_sizes,
    plan_total_nodes,
    reduce_node_input_hash,
)
from app.services.summary.provider import ChunkInput
from app.services.summary.settings import GROUP_MAX_CHARS, GROUP_MIN_CHARS


def _chunk(idx: int, *, title: str | None, chars: int) -> ChunkInput:
    return ChunkInput(
        chunk_id=f"c{idx}",
        section_title=title,
        text="가" * chars,
        page_start=idx,
        page_end=idx,
    )


class TestBoundedPacking:
    def test_short_sections_are_merged_instead_of_one_group_each(self):
        """제목이 매 청크마다 바뀌어도 GROUP_MIN_CHARS 전까지는 한 그룹으로 채운다."""
        chunks = [_chunk(i, title=f"제목 {i}", chars=500) for i in range(20)]
        groups = build_groups(chunks)
        # 제목마다 끊던 예전 동작이면 20그룹이 된다.
        assert len(groups) < 20
        assert sum(len(g.chunks) for g in groups) == 20

    def test_section_boundary_used_once_group_is_big_enough(self):
        """GROUP_MIN_CHARS를 넘긴 뒤 제목이 바뀌면 그 지점에서 끊는다."""
        chunks = [
            _chunk(0, title="A", chars=GROUP_MIN_CHARS),
            _chunk(1, title="B", chars=100),
        ]
        groups = build_groups(chunks)
        assert len(groups) == 2
        assert groups[0].section_title == "A"
        assert groups[1].section_title == "B"

    def test_never_exceeds_char_limit_when_splitting_is_possible(self):
        chunks = [_chunk(i, title="A", chars=1000) for i in range(30)]
        groups = build_groups(chunks)
        for g in groups:
            assert g.char_count <= GROUP_MAX_CHARS

    def test_oversized_single_chunk_gets_its_own_group(self):
        """상한보다 긴 단일 청크는 쪼개지 않고 혼자 그룹이 된다(다른 청크와 섞이지 않음)."""
        chunks = [
            _chunk(0, title="A", chars=100),
            _chunk(1, title="A", chars=GROUP_MAX_CHARS + 500),
            _chunk(2, title="A", chars=100),
        ]
        groups = build_groups(chunks)
        oversized = [g for g in groups if g.char_count > GROUP_MAX_CHARS]
        assert len(oversized) == 1
        assert [c.chunk_id for c in oversized[0].chunks] == ["c1"]

    def test_reading_order_is_preserved(self):
        chunks = [_chunk(i, title=f"제목 {i // 3}", chars=800) for i in range(24)]
        groups = build_groups(chunks)
        flat = [c.chunk_id for g in groups for c in g.chunks]
        assert flat == [f"c{i}" for i in range(24)]

    def test_blank_chunks_are_skipped(self):
        chunks = [
            _chunk(0, title="A", chars=100),
            ChunkInput(chunk_id="blank", section_title="A", text="   ", page_start=1, page_end=1),
        ]
        groups = build_groups(chunks)
        assert [c.chunk_id for g in groups for c in g.chunks] == ["c0"]

    def test_real_device_scale_shrinks_group_count(self):
        """실측 규모(4,265,890자)에서 그룹 수가 3,074개보다 크게 줄어든다.

        상한은 서버가 지정하는 num_ctx(8192)에 맞춰 GROUP_MAX_CHARS=6,000자다. 그 제약
        안에서 packing이 제 몫을 하는지 본다 — 제목마다 끊던 3,074그룹보다 크게 적어야
        한다. 현재 코드의 실측값은 797그룹(평균 5,357자)이므로 900을 넘으면 packing이
        60% 이상 잘게 쪼개지는 회귀다. 여유를 크게 두면(예: 1300) 그 회귀가 CI를 통과해
        대형 문서의 map 호출이 797회 → 1,290회로 늘고 요약 시간이 배로 늘어난다.
        """
        # 평균 536자 청크 7,965개 ≈ 4.27M자. 제목은 자주 바뀐다.
        chunks = [_chunk(i, title=f"제목 {i // 3}", chars=536) for i in range(7965)]
        groups = build_groups(chunks)
        assert len(groups) < 900, f"{len(groups)}그룹 — packing이 잘게 쪼개지고 있다"
        assert sum(len(g.chunks) for g in groups) == 7965

    def test_groups_are_packed_close_to_the_limit(self):
        """그룹이 절반만 차면 호출 수가 불필요하게 늘어난다 — 상한의 80% 이상을 채운다."""
        chunks = [_chunk(i, title=f"제목 {i // 3}", chars=536) for i in range(3000)]
        groups = build_groups(chunks)
        average = sum(g.char_count for g in groups) / len(groups)
        assert average >= GROUP_MAX_CHARS * 0.8, f"평균 {average:.0f}자 / 상한 {GROUP_MAX_CHARS}"


class TestLevelPlanning:
    def test_levels_shrink_until_fan_in(self):
        assert plan_level_sizes(711, 8) == [711, 89, 12, 2]

    def test_single_group_needs_no_reduce_level(self):
        assert plan_level_sizes(1, 8) == [1]

    def test_exactly_fan_in_needs_no_reduce_level(self):
        assert plan_level_sizes(8, 8) == [8]

    def test_total_nodes_matches_level_sum(self):
        assert plan_total_nodes(711, 8) == 711 + 89 + 12 + 2

    def test_empty_plan(self):
        assert plan_level_sizes(0, 8) == []

    def test_fan_in_must_allow_progress(self):
        with pytest.raises(ValueError):
            plan_level_sizes(10, 1)

    def test_batches_preserve_order_and_size(self):
        batches = fan_in_batches(list(range(10)), 4)
        assert batches == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]


class TestReuseKeys:
    def _ctx(self, **over):
        base = dict(
            provider_name="openai_compatible",
            model_name="qwen3:8b",
            prompt_version="3b-1",
            schema_version=1,
            learner_level="nursing_student",
            language="ko",
            source_revision=3,
            source_chunk_hash="abc",
        )
        base.update(over)
        return build_context_key(**base)

    def test_same_inputs_give_same_key(self):
        pairs = [("c1", "본문"), ("c2", "다음")]
        assert map_node_input_hash(self._ctx(), pairs) == map_node_input_hash(self._ctx(), pairs)

    def test_revision_change_breaks_reuse(self):
        pairs = [("c1", "본문")]
        assert map_node_input_hash(self._ctx(), pairs) != map_node_input_hash(
            self._ctx(source_revision=4), pairs
        )

    def test_chunk_hash_change_breaks_reuse(self):
        pairs = [("c1", "본문")]
        assert map_node_input_hash(self._ctx(), pairs) != map_node_input_hash(
            self._ctx(source_chunk_hash="zzz"), pairs
        )

    def test_model_and_prompt_change_break_reuse(self):
        pairs = [("c1", "본문")]
        base = map_node_input_hash(self._ctx(), pairs)
        assert base != map_node_input_hash(self._ctx(model_name="qwen3:14b"), pairs)
        assert base != map_node_input_hash(self._ctx(prompt_version="3b-2"), pairs)

    def test_learner_level_and_language_change_break_reuse(self):
        pairs = [("c1", "본문")]
        base = map_node_input_hash(self._ctx(), pairs)
        assert base != map_node_input_hash(self._ctx(learner_level="concise"), pairs)
        assert base != map_node_input_hash(self._ctx(language="en"), pairs)

    def test_chunk_id_change_breaks_reuse_even_with_same_text(self):
        """청크 재생성은 텍스트가 같아도 새 UUID·새 출처를 만든다 — 재사용하면 안 된다."""
        assert map_node_input_hash(self._ctx(), [("c1", "본문")]) != map_node_input_hash(
            self._ctx(), [("c2", "본문")]
        )

    def test_order_matters(self):
        ctx = self._ctx()
        assert map_node_input_hash(ctx, [("c1", "가"), ("c2", "나")]) != map_node_input_hash(
            ctx, [("c2", "나"), ("c1", "가")]
        )

    def test_reduce_key_depends_on_child_output(self):
        ctx = self._ctx()
        base = reduce_node_input_hash(ctx, 1, [("h1", "요약 A")])
        assert base != reduce_node_input_hash(ctx, 1, [("h1", "요약 B")])
        assert base != reduce_node_input_hash(ctx, 2, [("h1", "요약 A")])
