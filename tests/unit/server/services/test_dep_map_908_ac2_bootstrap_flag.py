"""
Story #908 AC2: Bootstrap flag enable_graph_channel_repair=False skips Phase 3.7.

Tests verify:
- flag=False: fixed[], errors[], journal are untouched by _run_phase37
- flag value is stored at construction as _enable_graph_channel_repair
- flag=False: Phase 4 (80%) and Phase 5 (90%) still execute normally
"""

import os
from typing import List
from unittest.mock import patch

from tests.unit.server.services.test_dep_map_908_builders import make_executor
from tests.unit.server.services.test_dep_map_908_helpers import make_minimal_dep_map

_PHASE4_PROGRESS = 80
_PHASE5_PROGRESS = 90


class TestAC2BootstrapFlag:
    """AC2: enable_graph_channel_repair flag gates Phase 3.7."""

    def test_flag_false_phase37_returns_without_modifying_state(self, tmp_path):
        """flag=False: _run_phase37 leaves fixed[], errors[], and journal unchanged."""
        output_dir = tmp_path / "dependency-map"
        make_minimal_dep_map(output_dir)

        journal_dir = tmp_path / "journal-dir"
        journal_dir.mkdir()
        journal_path = journal_dir / "dep_map_repair_journal.jsonl"

        executor = make_executor(enable_graph_channel_repair=False)
        fixed = ["pre-existing"]
        errors = ["pre-existing-error"]

        with patch.dict(os.environ, {"CIDX_DATA_DIR": str(journal_dir)}):
            executor._run_phase37(output_dir, fixed, errors)

        assert fixed == ["pre-existing"], f"fixed[] was modified: {fixed}"
        assert errors == ["pre-existing-error"], f"errors[] was modified: {errors}"
        assert not journal_path.exists() or journal_path.read_text() == "", (
            "Journal must not be written when flag=False"
        )

    def test_flag_stored_at_construction(self, tmp_path):
        """DepMapRepairExecutor stores _enable_graph_channel_repair at construction."""
        executor_true = make_executor(enable_graph_channel_repair=True)
        executor_false = make_executor(enable_graph_channel_repair=False)

        assert executor_true._enable_graph_channel_repair is True
        assert executor_false._enable_graph_channel_repair is False

    def test_flag_false_subsequent_phases_still_run(self, tmp_path):
        """flag=False: Phase 4 (80%) and Phase 5 (90%) still fire after Phase 3.7."""
        output_dir = tmp_path / "dependency-map"
        make_minimal_dep_map(output_dir)

        progress_events: List[tuple] = []
        executor = make_executor(
            enable_graph_channel_repair=False,
            progress_callback=lambda p, i: progress_events.append((p, i)),
        )

        from code_indexer.server.services.dep_map_health_detector import (
            DepMapHealthDetector,
        )

        health_report = DepMapHealthDetector().detect(output_dir)
        executor._run_branch_a_dep_map(output_dir, health_report, [], [])

        pcts = [p for p, _ in progress_events]
        assert _PHASE4_PROGRESS in pcts, (
            f"Phase 4 ({_PHASE4_PROGRESS}%) missing after flag=False. Events: {pcts}"
        )
        assert _PHASE5_PROGRESS in pcts, (
            f"Phase 5 ({_PHASE5_PROGRESS}%) missing after flag=False. Events: {pcts}"
        )


def test_factory_wires_enable_graph_channel_repair_from_config(tmp_path):
    """AC2 factory regression: _build_repair_executor reads enable_graph_channel_repair
    from ServerConfig and passes it to DepMapRepairExecutor at construction.

    Mocks get_config_service() so no real DB or filesystem is needed.
    Asserts executor._enable_graph_channel_repair matches the config flag.
    """
    from unittest.mock import MagicMock, patch

    from code_indexer.server.web.dependency_map_routes import _build_repair_executor

    fake_config = MagicMock()
    fake_config.enable_graph_channel_repair = False
    # Story #920: set per-type flags to None so executor defaults to "dry_run"
    fake_config.graph_repair_self_loop = None
    fake_config.graph_repair_malformed_yaml = None
    fake_config.graph_repair_garbage_domain = None
    fake_config.graph_repair_bidirectional_mismatch = None
    fake_config_service = MagicMock()
    fake_config_service.get_config.return_value = fake_config

    fake_dep_map_service = MagicMock()
    fake_dep_map_service._job_tracker = None
    fake_activity_journal = MagicMock()

    fake_golden_repo_manager = MagicMock()
    fake_golden_repo_manager.get_actual_repo_path.side_effect = (
        lambda alias: f"/mock/{alias}"
    )

    with (
        patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=fake_config_service,
        ),
        patch(
            "code_indexer.server.web.routes._get_golden_repo_manager",
            return_value=fake_golden_repo_manager,
        ),
    ):
        executor = _build_repair_executor(
            fake_dep_map_service, tmp_path, fake_activity_journal
        )

    assert executor._enable_graph_channel_repair is False, (
        f"Expected executor._enable_graph_channel_repair=False when config flag=False. "
        f"Got: {executor._enable_graph_channel_repair!r}"
    )
