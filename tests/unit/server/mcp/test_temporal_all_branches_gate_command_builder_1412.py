"""Unit tests for Story #1412 - defense-in-depth #3: MCP
_build_temporal_index_cmd (code_indexer.server.mcp.handlers.repos) must NOT
append --all-branches when the server-wide temporal_all_branches_enabled
gate is off, even if temporal_options.all_branches is True. A WARNING must
be logged recording the gate-driven downgrade to single-branch.
"""

import logging

from code_indexer.server.mcp.handlers.repos import _build_temporal_index_cmd


class TestBuildTemporalIndexCmdGateOff:
    """Defense-in-depth: gate off (default) must skip --all-branches + WARNING."""

    def test_gate_off_default_omits_flag_when_all_branches_true(self) -> None:
        cmd = _build_temporal_index_cmd(
            clear=False, temporal_options={"all_branches": True}
        )
        assert "--all-branches" not in cmd, (
            f"Default gate (off) must omit '--all-branches' even with "
            f"all_branches=True. Got: {cmd}"
        )

    def test_gate_off_explicit_omits_flag_when_all_branches_true(self) -> None:
        cmd = _build_temporal_index_cmd(
            clear=False,
            temporal_options={"all_branches": True},
            all_branches_gate_enabled=False,
        )
        assert "--all-branches" not in cmd

    def test_gate_off_logs_warning_naming_alias(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            _build_temporal_index_cmd(
                clear=False,
                temporal_options={"all_branches": True},
                all_branches_gate_enabled=False,
                alias="my-golden-repo",
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "my-golden-repo" in r.getMessage() and "all_branches" in r.getMessage()
            for r in warnings
        ), (
            f"Expected a WARNING naming the repo. Got: {[r.getMessage() for r in warnings]}"
        )


class TestBuildTemporalIndexCmdGateOn:
    """Gate on -> passthrough, unchanged behavior."""

    def test_gate_on_includes_flag_when_all_branches_true(self) -> None:
        cmd = _build_temporal_index_cmd(
            clear=False,
            temporal_options={"all_branches": True},
            all_branches_gate_enabled=True,
        )
        assert "--all-branches" in cmd

    def test_gate_on_omits_flag_when_all_branches_false(self) -> None:
        cmd = _build_temporal_index_cmd(
            clear=False,
            temporal_options={"all_branches": False},
            all_branches_gate_enabled=True,
        )
        assert "--all-branches" not in cmd
