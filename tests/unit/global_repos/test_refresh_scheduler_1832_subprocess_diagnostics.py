"""Bug #1832: RefreshScheduler._attempt_reclone's "Auto re-clone FAILED"
critical log (line ~1187) used only `clone_result.stderr` -- when the
failing `git clone` subprocess writes its real diagnostic to stdout
instead, the message degrades to "Auto re-clone FAILED for <alias>: " with
an empty tail.

Discriminating case (AC5): stderr EMPTY, stdout NON-EMPTY. A test using a
non-empty stderr would pass before the fix and prove nothing.

Mirrors the RefreshScheduler construction pattern already established in
test_refresh_scheduler_backoff.py.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

ALIAS = "sales-global"
REPO_URL = "https://gitlab.example.com/group/sales.git"
_DISCRIMINATING_STDOUT = "fatal: unable to connect to gitlab.example.com"


@pytest.fixture
def scheduler(tmp_path):
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir()

    config = Mock()
    config.get_global_refresh_interval.return_value = 3600

    registry = Mock()
    registry.get_global_repo.return_value = {
        "alias_name": ALIAS,
        "repo_url": REPO_URL,
        "enable_temporal": False,
        "enable_scip": False,
    }

    return RefreshScheduler(
        golden_repos_dir=str(golden_dir),
        config_source=config,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=registry,
    )


class TestAttemptRecloneDiagnostic:
    def test_empty_stderr_nonempty_stdout_surfaces_in_critical_log(
        self, scheduler, tmp_path, caplog
    ) -> None:
        master_path = str(tmp_path / "golden-repos" / "sales")

        with (
            patch(
                "code_indexer.global_repos.refresh_scheduler.subprocess.run",
                return_value=Mock(
                    args=["git", "clone", REPO_URL, "dest"],
                    returncode=1,
                    stdout=_DISCRIMINATING_STDOUT,
                    stderr="",
                ),
            ),
            caplog.at_level(logging.CRITICAL),
        ):
            result = scheduler._attempt_reclone(ALIAS, REPO_URL, master_path)

        assert result is False

        criticals = [
            record.message
            for record in caplog.records
            if "Auto re-clone FAILED" in record.message
        ]
        assert criticals, f"expected an Auto re-clone FAILED log, got: {caplog.records}"
        assert _DISCRIMINATING_STDOUT in criticals[0], (
            f"stdout diagnostic missing from log: {criticals[0]!r}"
        )
