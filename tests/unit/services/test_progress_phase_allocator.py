"""
Unit tests for ProgressPhaseAllocator.

Story #480: Real-Time Progress Reporting for Index Rebuild Jobs
TDD: These tests define the expected behavior BEFORE implementation.

Tests cover:
- calculate_weights with various index type combinations
- Dynamic weight allocation for different repo sizes
- map_phase_progress correct global range mapping
- phase_start / phase_end convenience methods
- Edge cases (zero total, single phase)
- Phase ranges sum to 100%
- JSON progress line parsing
- CLI --progress-json flag registration
"""

import json
from typing import List, Optional

import pytest

# The module under test (will fail until implemented)
from code_indexer.services.progress_phase_allocator import (
    ProgressPhaseAllocator,
    Phase,
    COST_PER_FILE,
    COST_PER_COMMIT,
    COST_FTS_FIXED,
    COST_SCIP_FIXED,
    COST_COW_FIXED,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_allocator(
    index_types: List[str],
    file_count: int = 100,
    commit_count: int = 50,
    max_commits: Optional[int] = None,
) -> ProgressPhaseAllocator:
    """Helper: create and configure an allocator."""
    allocator = ProgressPhaseAllocator()
    allocator.calculate_weights(index_types, file_count, commit_count, max_commits)
    return allocator


# ---------------------------------------------------------------------------
# Constants validation
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify cost constants are as specified in story."""

    def test_cost_per_file(self):
        assert COST_PER_FILE == 1.0

    def test_cost_per_commit(self):
        assert COST_PER_COMMIT == 2.5

    def test_cost_fts_fixed(self):
        assert COST_FTS_FIXED == 50

    def test_cost_scip_fixed(self):
        assert COST_SCIP_FIXED == 30

    def test_cost_cow_fixed(self):
        assert COST_COW_FIXED == 20


# ---------------------------------------------------------------------------
# Phase dataclass
# ---------------------------------------------------------------------------


class TestPhaseDataclass:
    """Verify Phase namedtuple/dataclass has the right fields."""

    def test_phase_has_name(self):
        p = Phase(name="semantic", weight=0.5, range_start=0.0, range_end=50.0)
        assert p.name == "semantic"

    def test_phase_has_weight(self):
        p = Phase(name="semantic", weight=0.5, range_start=0.0, range_end=50.0)
        assert p.weight == pytest.approx(0.5)

    def test_phase_has_range_start(self):
        p = Phase(name="semantic", weight=0.5, range_start=0.0, range_end=50.0)
        assert p.range_start == pytest.approx(0.0)

    def test_phase_has_range_end(self):
        p = Phase(name="semantic", weight=0.5, range_start=0.0, range_end=50.0)
        assert p.range_end == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Semantic-only allocation
# ---------------------------------------------------------------------------


class TestSemanticOnly:
    """Semantic + CoW (always present for git repos)."""

    def test_phases_present(self):
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        names = [p.name for p in allocator.phases]
        assert "semantic" in names
        assert "cow" in names

    def test_no_temporal_phase(self):
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        names = [p.name for p in allocator.phases]
        assert "temporal" not in names

    def test_ranges_sum_to_100(self):
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        total = sum(p.range_end - p.range_start for p in allocator.phases)
        assert total == pytest.approx(100.0, abs=0.01)

    def test_first_range_starts_at_zero(self):
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        assert allocator.phases[0].range_start == pytest.approx(0.0)

    def test_last_range_ends_at_100(self):
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        assert allocator.phases[-1].range_end == pytest.approx(100.0, abs=0.01)

    def test_semantic_dominates_large_repo(self):
        # 5000 files * 1.0 = 5000 semantic cost, CoW = 20 fixed
        # semantic weight = 5000 / 5020 ≈ 0.996
        allocator = make_allocator(["semantic"], file_count=5000, commit_count=0)
        semantic_phase = next(p for p in allocator.phases if p.name == "semantic")
        assert semantic_phase.weight > 0.99


# ---------------------------------------------------------------------------
# Temporal-only allocation
# ---------------------------------------------------------------------------


class TestTemporalOnly:
    """Temporal + CoW."""

    def test_phases_present(self):
        allocator = make_allocator(["temporal"], file_count=0, commit_count=200)
        names = [p.name for p in allocator.phases]
        assert "temporal" in names
        assert "cow" in names

    def test_ranges_sum_to_100(self):
        allocator = make_allocator(["temporal"], file_count=0, commit_count=200)
        total = sum(p.range_end - p.range_start for p in allocator.phases)
        assert total == pytest.approx(100.0, abs=0.01)

    def test_temporal_cost_uses_commit_count(self):
        # 200 commits * 2.5 = 500, CoW = 20 → temporal_weight = 500/520 ≈ 0.9615
        allocator = make_allocator(["temporal"], file_count=0, commit_count=200)
        temporal_phase = next(p for p in allocator.phases if p.name == "temporal")
        expected_weight = (200 * COST_PER_COMMIT) / (
            200 * COST_PER_COMMIT + COST_COW_FIXED
        )
        assert temporal_phase.weight == pytest.approx(expected_weight, abs=0.001)


# ---------------------------------------------------------------------------
# Semantic + Temporal (composite)
# ---------------------------------------------------------------------------


class TestSemanticPlusTemporal:
    """AC3: Composite job distributes progress across phases proportionally."""

    def test_all_three_phases_present(self):
        allocator = make_allocator(
            ["semantic", "temporal"], file_count=500, commit_count=200
        )
        names = [p.name for p in allocator.phases]
        assert "semantic" in names
        assert "temporal" in names
        assert "cow" in names

    def test_ranges_sum_to_100(self):
        allocator = make_allocator(
            ["semantic", "temporal"], file_count=500, commit_count=200
        )
        total = sum(p.range_end - p.range_start for p in allocator.phases)
        assert total == pytest.approx(100.0, abs=0.01)

    def test_execution_order_semantic_first(self):
        allocator = make_allocator(
            ["semantic", "temporal"], file_count=500, commit_count=200
        )
        names = [p.name for p in allocator.phases]
        assert names.index("semantic") < names.index("temporal")

    def test_cow_always_last(self):
        allocator = make_allocator(
            ["semantic", "temporal"], file_count=500, commit_count=200
        )
        assert allocator.phases[-1].name == "cow"

    def test_phases_contiguous(self):
        """Each phase range_start must equal previous phase range_end."""
        allocator = make_allocator(
            ["semantic", "temporal"], file_count=500, commit_count=200
        )
        for i in range(1, len(allocator.phases)):
            assert allocator.phases[i].range_start == pytest.approx(
                allocator.phases[i - 1].range_end, abs=0.001
            )


# ---------------------------------------------------------------------------
# AC4: Dynamic weight allocation for various repo sizes
# ---------------------------------------------------------------------------


class TestDynamicWeightAllocation:
    """
    AC4: Dynamic weight allocation adapts to repo characteristics.

    Story specifies approximate percentages:
    - Small repo: 200 files, 150 commits
      semantic ≈ 34%, temporal ≈ 63%
    - Large code repo: 5000 files, 500 commits
      semantic ≈ 80%, temporal ≈ 19%
    - Commit-heavy repo: 200 files, 88000 commits
      temporal > 99%
    """

    def test_small_repo_semantic_approx_34_percent(self):
        # 200 * 1.0 = 200 semantic, 150 * 2.5 = 375 temporal, 20 cow
        # total = 595, semantic_pct = 200/595 ≈ 33.6% ≈ 34%
        allocator = make_allocator(
            ["semantic", "temporal"], file_count=200, commit_count=150
        )
        semantic = next(p for p in allocator.phases if p.name == "semantic")
        assert abs(semantic.weight * 100 - 33.6) < 2.0  # within 2% of expected

    def test_small_repo_temporal_approx_63_percent(self):
        # 375 / 595 ≈ 63%
        allocator = make_allocator(
            ["semantic", "temporal"], file_count=200, commit_count=150
        )
        temporal = next(p for p in allocator.phases if p.name == "temporal")
        assert abs(temporal.weight * 100 - 63.0) < 2.0

    def test_large_repo_semantic_approx_80_percent(self):
        # 5000 * 1.0 = 5000 semantic, 500 * 2.5 = 1250 temporal, 20 cow
        # total = 6270, semantic_pct = 5000/6270 ≈ 79.7% ≈ 80%
        allocator = make_allocator(
            ["semantic", "temporal"], file_count=5000, commit_count=500
        )
        semantic = next(p for p in allocator.phases if p.name == "semantic")
        assert abs(semantic.weight * 100 - 79.7) < 2.0

    def test_large_repo_temporal_approx_19_percent(self):
        # 1250 / 6270 ≈ 19.9% ≈ 19%
        allocator = make_allocator(
            ["semantic", "temporal"], file_count=5000, commit_count=500
        )
        temporal = next(p for p in allocator.phases if p.name == "temporal")
        assert abs(temporal.weight * 100 - 19.9) < 2.0

    def test_commit_heavy_repo_temporal_dominates(self):
        # 200 files * 1.0 = 200, 88000 commits * 2.5 = 220000, 20 cow
        # temporal_pct = 220000 / 220220 ≈ 99.9%
        allocator = make_allocator(
            ["semantic", "temporal"], file_count=200, commit_count=88000
        )
        temporal = next(p for p in allocator.phases if p.name == "temporal")
        assert temporal.weight > 0.99

    def test_max_commits_caps_temporal_cost(self):
        # max_commits=100 caps effective commits: min(88000, 100) = 100
        # temporal_cost = 100 * 2.5 = 250, semantic_cost = 200 * 1.0 = 200
        # total = 470, temporal_pct ≈ 53%
        allocator = make_allocator(
            ["semantic", "temporal"],
            file_count=200,
            commit_count=88000,
            max_commits=100,
        )
        temporal = next(p for p in allocator.phases if p.name == "temporal")
        expected_weight = (100 * COST_PER_COMMIT) / (
            200 + 100 * COST_PER_COMMIT + COST_COW_FIXED
        )
        assert temporal.weight == pytest.approx(expected_weight, abs=0.001)


# ---------------------------------------------------------------------------
# FTS and SCIP phases
# ---------------------------------------------------------------------------


class TestFtsAndScipPhases:
    """AC5: FTS and SCIP occupy their allocated range (coarse start/end markers)."""

    def test_fts_phase_present_when_requested(self):
        allocator = make_allocator(["semantic", "fts"], file_count=500, commit_count=0)
        names = [p.name for p in allocator.phases]
        assert "fts" in names

    def test_scip_phase_present_when_requested(self):
        allocator = make_allocator(["semantic", "scip"], file_count=500, commit_count=0)
        names = [p.name for p in allocator.phases]
        assert "scip" in names

    def test_all_types_have_correct_phases(self):
        allocator = make_allocator(
            ["semantic", "fts", "temporal", "scip"],
            file_count=500,
            commit_count=200,
        )
        names = [p.name for p in allocator.phases]
        assert "semantic" in names
        assert "fts" in names
        assert "temporal" in names
        assert "scip" in names
        assert "cow" in names

    def test_all_types_ranges_sum_to_100(self):
        allocator = make_allocator(
            ["semantic", "fts", "temporal", "scip"],
            file_count=500,
            commit_count=200,
        )
        total = sum(p.range_end - p.range_start for p in allocator.phases)
        assert total == pytest.approx(100.0, abs=0.01)

    def test_fts_uses_fixed_cost(self):
        # FTS should use COST_FTS_FIXED = 50
        allocator = make_allocator(["fts"], file_count=0, commit_count=0)
        fts_phase = next(p for p in allocator.phases if p.name == "fts")
        expected_weight = COST_FTS_FIXED / (COST_FTS_FIXED + COST_COW_FIXED)
        assert fts_phase.weight == pytest.approx(expected_weight, abs=0.001)

    def test_scip_uses_fixed_cost(self):
        # SCIP should use COST_SCIP_FIXED = 30
        allocator = make_allocator(["scip"], file_count=0, commit_count=0)
        scip_phase = next(p for p in allocator.phases if p.name == "scip")
        expected_weight = COST_SCIP_FIXED / (COST_SCIP_FIXED + COST_COW_FIXED)
        assert scip_phase.weight == pytest.approx(expected_weight, abs=0.001)


# ---------------------------------------------------------------------------
# map_phase_progress
# ---------------------------------------------------------------------------


class TestMapPhaseProgress:
    """AC1/AC2: map_phase_progress correctly maps local progress to global 0-100."""

    def test_start_of_phase_maps_to_range_start(self):
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        result = allocator.map_phase_progress("semantic", 0, 100)
        semantic_phase = next(p for p in allocator.phases if p.name == "semantic")
        assert result == pytest.approx(semantic_phase.range_start, abs=0.01)

    def test_end_of_phase_maps_to_range_end(self):
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        result = allocator.map_phase_progress("semantic", 100, 100)
        semantic_phase = next(p for p in allocator.phases if p.name == "semantic")
        assert result == pytest.approx(semantic_phase.range_end, abs=0.01)

    def test_midpoint_maps_to_midpoint_of_range(self):
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        semantic_phase = next(p for p in allocator.phases if p.name == "semantic")
        expected_mid = (semantic_phase.range_start + semantic_phase.range_end) / 2.0
        result = allocator.map_phase_progress("semantic", 50, 100)
        assert result == pytest.approx(expected_mid, abs=0.01)

    def test_zero_total_returns_range_start(self):
        """Edge case: if total=0, return range_start (not NaN or error)."""
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        semantic_phase = next(p for p in allocator.phases if p.name == "semantic")
        result = allocator.map_phase_progress("semantic", 0, 0)
        assert result == pytest.approx(semantic_phase.range_start, abs=0.01)

    def test_second_phase_mapping(self):
        """Temporal phase should map into its range, not from 0."""
        allocator = make_allocator(
            ["semantic", "temporal"], file_count=500, commit_count=200
        )
        temporal_phase = next(p for p in allocator.phases if p.name == "temporal")
        # Progress at start of temporal phase
        result = allocator.map_phase_progress("temporal", 0, 100)
        assert result == pytest.approx(temporal_phase.range_start, abs=0.01)
        # Progress is never < temporal range_start
        assert result >= temporal_phase.range_start - 0.01

    def test_result_is_float(self):
        allocator = make_allocator(["semantic"], file_count=100, commit_count=0)
        result = allocator.map_phase_progress("semantic", 50, 100)
        assert isinstance(result, float)

    def test_unknown_phase_raises_value_error(self):
        allocator = make_allocator(["semantic"], file_count=100, commit_count=0)
        with pytest.raises((ValueError, KeyError)):
            allocator.map_phase_progress("nonexistent", 50, 100)


# ---------------------------------------------------------------------------
# phase_start / phase_end convenience methods
# ---------------------------------------------------------------------------


class TestPhaseStartEnd:
    """Convenience methods for coarse start/end reporting (FTS, SCIP, CoW)."""

    def test_phase_start_returns_range_start(self):
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        semantic_phase = next(p for p in allocator.phases if p.name == "semantic")
        assert allocator.phase_start("semantic") == pytest.approx(
            semantic_phase.range_start, abs=0.01
        )

    def test_phase_end_returns_range_end(self):
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        semantic_phase = next(p for p in allocator.phases if p.name == "semantic")
        assert allocator.phase_end("semantic") == pytest.approx(
            semantic_phase.range_end, abs=0.01
        )

    def test_cow_phase_start(self):
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        cow_phase = next(p for p in allocator.phases if p.name == "cow")
        assert allocator.phase_start("cow") == pytest.approx(
            cow_phase.range_start, abs=0.01
        )

    def test_cow_phase_end_is_100(self):
        allocator = make_allocator(["semantic"], file_count=500, commit_count=0)
        assert allocator.phase_end("cow") == pytest.approx(100.0, abs=0.01)

    def test_phase_start_unknown_raises(self):
        allocator = make_allocator(["semantic"], file_count=100, commit_count=0)
        with pytest.raises((ValueError, KeyError)):
            allocator.phase_start("nonexistent")

    def test_phase_end_unknown_raises(self):
        allocator = make_allocator(["semantic"], file_count=100, commit_count=0)
        with pytest.raises((ValueError, KeyError)):
            allocator.phase_end("nonexistent")


# ---------------------------------------------------------------------------
# Execution order is canonical
# ---------------------------------------------------------------------------


class TestExecutionOrder:
    """Phases must appear in the defined execution order."""

    def test_semantic_before_fts(self):
        allocator = make_allocator(["semantic", "fts"], file_count=100, commit_count=0)
        names = [p.name for p in allocator.phases]
        assert names.index("semantic") < names.index("fts")

    def test_temporal_before_scip(self):
        allocator = make_allocator(["temporal", "scip"], file_count=0, commit_count=100)
        names = [p.name for p in allocator.phases]
        assert names.index("temporal") < names.index("scip")

    def test_cow_is_always_last(self):
        for types in [
            ["semantic"],
            ["temporal"],
            ["fts"],
            ["scip"],
            ["semantic", "temporal"],
            ["semantic", "fts", "temporal", "scip"],
        ]:
            allocator = make_allocator(types, file_count=100, commit_count=100)
            assert allocator.phases[-1].name == "cow"


# ---------------------------------------------------------------------------
# JSON progress line parsing (AC8 and AC9)
# ---------------------------------------------------------------------------


class TestJsonProgressLineParsing:
    """
    AC8: Non-JSON stdout lines are safely ignored by line reader.

    These tests verify the parse_progress_line helper function that
    golden_repo_manager.py will use to process subprocess stdout lines.
    """

    def test_parse_valid_json_line(self):
        from code_indexer.services.progress_phase_allocator import parse_progress_line

        line = json.dumps({"current": 50, "total": 100, "info": "processing files"})
        result = parse_progress_line(line)
        assert result is not None
        assert result["current"] == 50
        assert result["total"] == 100
        assert result["info"] == "processing files"

    def test_parse_valid_json_missing_info(self):
        from code_indexer.services.progress_phase_allocator import parse_progress_line

        line = json.dumps({"current": 10, "total": 50})
        result = parse_progress_line(line)
        assert result is not None
        assert result["current"] == 10
        assert result["total"] == 50
        assert result.get("info", "") == ""

    def test_parse_non_json_returns_none(self):
        """AC8: Non-JSON lines must be skipped (return None, no exception)."""
        from code_indexer.services.progress_phase_allocator import parse_progress_line

        result = parse_progress_line("WARNING: some log message")
        assert result is None

    def test_parse_empty_line_returns_none(self):
        from code_indexer.services.progress_phase_allocator import parse_progress_line

        result = parse_progress_line("")
        assert result is None

    def test_parse_whitespace_line_returns_none(self):
        from code_indexer.services.progress_phase_allocator import parse_progress_line

        result = parse_progress_line("   \n")
        assert result is None

    def test_parse_partial_json_returns_none(self):
        from code_indexer.services.progress_phase_allocator import parse_progress_line

        result = parse_progress_line('{"current": 10,')
        assert result is None

    def test_parse_json_missing_current_returns_none(self):
        """Lines with missing required fields should be skipped."""
        from code_indexer.services.progress_phase_allocator import parse_progress_line

        line = json.dumps({"total": 100, "info": "test"})
        result = parse_progress_line(line)
        assert result is None

    def test_parse_json_missing_total_returns_none(self):
        from code_indexer.services.progress_phase_allocator import parse_progress_line

        line = json.dumps({"current": 10, "info": "test"})
        result = parse_progress_line(line)
        assert result is None

    def test_parse_json_non_object_returns_none(self):
        from code_indexer.services.progress_phase_allocator import parse_progress_line

        result = parse_progress_line(json.dumps([1, 2, 3]))
        assert result is None

    def test_parse_returns_dict(self):
        from code_indexer.services.progress_phase_allocator import parse_progress_line

        line = json.dumps({"current": 5, "total": 10, "info": "x"})
        result = parse_progress_line(line)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# calculate_weights called multiple times (idempotency)
# ---------------------------------------------------------------------------


class TestRecalculation:
    """calculate_weights can be called multiple times safely."""

    def test_recalculate_resets_phases(self):
        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(["semantic"], 100, 0)
        first_count = len(allocator.phases)

        allocator.calculate_weights(["semantic", "temporal"], 100, 50)
        second_count = len(allocator.phases)

        assert second_count > first_count  # temporal adds a phase

    def test_recalculate_phases_still_sum_to_100(self):
        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(["semantic"], 100, 0)
        allocator.calculate_weights(["semantic", "temporal", "fts"], 500, 200)
        total = sum(p.range_end - p.range_start for p in allocator.phases)
        assert total == pytest.approx(100.0, abs=0.01)


# ---------------------------------------------------------------------------
# CLI --progress-json flag registration (AC6)
# ---------------------------------------------------------------------------


class TestCliProgressJsonFlag:
    """
    AC6: --progress-json CLI flag is registered on the index command.

    We test the click command object directly without running it.
    """

    def test_progress_json_option_registered(self):
        """The --progress-json flag must be a registered option on cidx index."""
        from code_indexer.cli import cli

        # Get the index command
        index_cmd = cli.commands.get("index")
        assert index_cmd is not None, "index command must exist"

        # Find --progress-json option
        option_names = []
        for param in index_cmd.params:
            option_names.extend(param.opts)

        assert "--progress-json" in option_names, (
            f"--progress-json must be a registered option. Found: {option_names}"
        )

    def test_progress_json_is_flag(self):
        """--progress-json should be a boolean flag (is_flag=True)."""
        from code_indexer.cli import cli
        import click

        index_cmd = cli.commands.get("index")
        assert index_cmd is not None

        progress_json_param = None
        for param in index_cmd.params:
            if "--progress-json" in param.opts:
                progress_json_param = param
                break

        assert progress_json_param is not None, "--progress-json option not found"
        assert isinstance(progress_json_param, click.Option)
        assert progress_json_param.is_flag is True


# ---------------------------------------------------------------------------
# Progress JSON emission logic (AC6)
# ---------------------------------------------------------------------------


class TestProgressJsonEmission:
    """
    AC6: When --progress-json is active, each progress update emits a JSON line.
    These tests verify the format of emitted lines.
    """

    def test_json_output_has_required_fields(self, capsys):
        """Emitting a progress update must produce parseable JSON with current/total/info."""
        from code_indexer.services.progress_phase_allocator import emit_progress_json

        emit_progress_json(current=50, total=100, info="indexing files")
        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().split("\n") if line.strip()]
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["current"] == 50
        assert data["total"] == 100
        assert data["info"] == "indexing files"

    def test_json_output_is_flushed(self, capsys):
        """Output must be flushed immediately (flush=True)."""
        from code_indexer.services.progress_phase_allocator import emit_progress_json

        # Just verify it doesn't hang and produces output
        emit_progress_json(current=1, total=10, info="test")
        captured = capsys.readouterr()
        assert captured.out.strip() != ""

    def test_json_output_one_line_per_call(self, capsys):
        """Each call produces exactly one JSON line."""
        from code_indexer.services.progress_phase_allocator import emit_progress_json

        emit_progress_json(current=10, total=100, info="a")
        emit_progress_json(current=20, total=100, info="b")
        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().split("\n") if line.strip()]
        assert len(lines) == 2
