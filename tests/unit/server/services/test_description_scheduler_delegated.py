"""Description refresh under a delegated content backend with a LOCAL agent:
not started.

Every pass the scheduler runs (broken-lifecycle and terse-description
backfills, the periodic refresh) hands a golden repo to the lifecycle agent,
which, being the built-in local CLI, refuses under delegated -- the repository
is not on this pod (an invoker that provisions it itself runs them: see
test_orchestrator_golden_repo_source.py). Starting
them would fail a job per alias every cycle with the same refusal. So start()
does the bookkeeping (no repository access) and says once why the rest did not
start. Locally nothing changes.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services import description_refresh_scheduler as drs
from code_indexer.server.startup import content_backend_wiring


def _scheduler(tmp_path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    config = SimpleNamespace(
        claude_integration_config=SimpleNamespace(
            description_refresh_enabled=True,
            description_refresh_interval_hours=24,
            max_concurrent_claude_cli=1,
        )
    )
    config_manager = MagicMock()
    config_manager.load_config.return_value = config
    scheduler = drs.DescriptionRefreshScheduler(
        db_path=str(tmp_path / "db.sqlite"), config_manager=config_manager
    )
    calls = MagicMock()
    for name in (
        "_migrate_global_suffix_filenames",
        "reconcile_orphan_tracking",
        "_reconcile_stale_next_run_rows",
        "reconcile_broken_lifecycle_metadata",
        "reconcile_terse_descriptions",
        "_run_loop",
    ):
        monkeypatch.setattr(scheduler, name, getattr(calls, name))
    scheduler._calls = calls  # type: ignore[attr-defined]
    return scheduler


def _park(monkeypatch: pytest.MonkeyPatch, *, local: bool) -> None:
    backend = SimpleNamespace(is_local=local)
    monkeypatch.setattr(
        content_backend_wiring, "server_content_backend", lambda: backend
    )


def test_local_starts_every_pass(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _park(monkeypatch, local=True)
    scheduler = _scheduler(tmp_path, monkeypatch)
    scheduler.start()
    scheduler._thread.join(timeout=5)
    called = {c[0] for c in scheduler._calls.method_calls}
    assert {
        "reconcile_broken_lifecycle_metadata",
        "reconcile_terse_descriptions",
        "_run_loop",
    } <= called


def test_delegated_starts_no_analysis_and_says_why(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _park(monkeypatch, local=False)
    scheduler = _scheduler(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger=drs.logger.name):
        scheduler.start()
    called = {c[0] for c in scheduler._calls.method_calls}
    assert called == {
        "_migrate_global_suffix_filenames",
        "reconcile_orphan_tracking",
        "_reconcile_stale_next_run_rows",
    }
    assert scheduler._thread is None
    assert any(
        "NOT started" in r.getMessage()
        and "content_backend='delegated'" in r.getMessage()
        for r in caplog.records
    )
