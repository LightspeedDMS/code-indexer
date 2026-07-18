"""Tests for Story #1404 wiring at the launch site
server/mcp/handlers/repos.py::_build_temporal_index_cmd:

1. Bug fix (spec-corrections item 1, bonus): the per-repo since_date option
   used to emit "--since <value>" but cli.py only defines "--since-date" --
   every provider temporal rebuild with a per-repo since_date crashed the
   child process with an invalid-option error. Regression test proves the
   correct flag name is used.

2. New `global_floor_date` parameter threads the global temporal indexing
   floor date into the constructed command via the "more restrictive wins"
   precedence helper (resolve_effective_floor_date) -- exactly one
   --since-date is ever emitted, never two, and the flag is omitted
   entirely when both global and per-repo dates are unset (Scenario 5
   no-op preserved).
"""

from code_indexer.server.mcp.handlers.repos import _build_temporal_index_cmd


class TestBuildTemporalIndexCmdSinceDateFlagNameBugFix:
    """Regression test for the --since -> --since-date bug (spec-corrections
    item 1 bonus fix)."""

    def test_per_repo_since_date_uses_correct_flag_name(self) -> None:
        cmd = _build_temporal_index_cmd(
            clear=False, temporal_options={"since_date": "2025-01-01"}
        )
        assert "--since-date" in cmd, (
            f"Expected the correct '--since-date' flag (cli.py only "
            f"defines --since-date, never --since). Got: {cmd}"
        )
        assert "--since" not in cmd, (
            f"'--since' (wrong flag name) must never appear. Got: {cmd}"
        )
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-01-01"


class TestBuildTemporalIndexCmdGlobalFloorDateOnly:
    def test_global_floor_date_applied_when_no_per_repo_override(self) -> None:
        cmd = _build_temporal_index_cmd(
            clear=False,
            temporal_options={},
            global_floor_date="2025-01-01",
        )
        assert "--since-date" in cmd
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-01-01"

    def test_global_floor_date_applied_with_none_temporal_options(self) -> None:
        cmd = _build_temporal_index_cmd(
            clear=False,
            temporal_options=None,  # type: ignore[arg-type]
            global_floor_date="2025-01-01",
        )
        assert "--since-date" in cmd
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-01-01"


class TestBuildTemporalIndexCmdPrecedenceScenario6:
    """Scenario 6: 'more restrictive wins' -- exactly one --since-date is
    ever emitted."""

    def test_per_repo_more_restrictive_than_global_wins(self) -> None:
        cmd = _build_temporal_index_cmd(
            clear=False,
            temporal_options={"since_date": "2025-06-01"},
            global_floor_date="2024-01-01",
        )
        assert cmd.count("--since-date") == 1
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-06-01"

    def test_global_more_restrictive_than_per_repo_wins(self) -> None:
        cmd = _build_temporal_index_cmd(
            clear=False,
            temporal_options={"since_date": "2024-01-01"},
            global_floor_date="2025-06-01",
        )
        assert cmd.count("--since-date") == 1
        idx = cmd.index("--since-date")
        assert cmd[idx + 1] == "2025-06-01"


class TestBuildTemporalIndexCmdScenario5NoOp:
    """Unset floor = full-history no-op: no --since-date flag at all."""

    def test_both_unset_omits_flag_entirely(self) -> None:
        cmd = _build_temporal_index_cmd(clear=False, temporal_options={})
        assert "--since-date" not in cmd

    def test_both_unset_with_none_temporal_options_omits_flag(self) -> None:
        cmd = _build_temporal_index_cmd(
            clear=False,
            temporal_options=None,  # type: ignore[arg-type]
        )
        assert "--since-date" not in cmd
