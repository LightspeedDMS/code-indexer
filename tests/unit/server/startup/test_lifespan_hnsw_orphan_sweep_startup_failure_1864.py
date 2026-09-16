"""Bug #1864: a startup failure of the on-by-default HNSW orphan repair
fleet sweep must not be a WARNING that scrolls past.

Before this fix ``lifespan.py``'s construct-and-``start()`` block wrapped
both of its explicit ``raise RuntimeError`` dependency guards
(``backend_registry is None``; ``golden_repo_manager.activated_repo_manager
is None``) in one ``except Exception`` whose ONLY consequence was
``logger.warning(format_error_log("APP-GENERAL-090", ...))``. The server then
booted reporting healthy, the sweep never ran, and the sole trace was a
single boot log line. CLAUDE.md documents this subsystem as "Ships ON by
default", so an operator has every reason to assume it is running.

Two things change here, both still NON-FATAL to boot (degrading is correct;
staying silent is not):

  1. The failure is recorded on ``app.state`` so the admin stats endpoint can
     report it on every request instead of only at boot.
  2. It is logged at ERROR, not WARNING. That is a real escalation in this
     codebase, not cosmetics: ERROR rows are what ``admin_logs_query`` and
     the Post-E2E log-audit gate (``tests/e2e/log_audit_gate.py``) select on,
     so the condition now fails a phase instead of being invisible.

The block is extracted to a module-level helper for the same reason
``_wire_query_tracker_into_semantic_query_manager`` and
``_build_job_counts_callback`` are: it makes the real production path
directly testable rather than reachable only through the whole async
lifespan.
"""

import logging
from types import SimpleNamespace
from typing import List, Optional

import pytest


class _FakeActivatedRepoManager:
    def list_all_activated_repositories(self) -> List[str]:
        return []


class _FakeGoldenRepoManager:
    def __init__(self, activated_repo_manager: Optional[object]) -> None:
        self.activated_repo_manager = activated_repo_manager

    def list_golden_repos(self) -> List[str]:
        return []


def _make_app() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace())


def _start(
    app: SimpleNamespace,
    *,
    golden_repo_manager: _FakeGoldenRepoManager,
    backend_registry: Optional[object],
) -> Optional[object]:
    from code_indexer.server.startup.lifespan import (
        _start_hnsw_orphan_repair_sweep_scheduler,
    )

    return _start_hnsw_orphan_repair_sweep_scheduler(
        app,
        golden_repo_manager=golden_repo_manager,
        backend_registry=backend_registry,
        background_job_manager=SimpleNamespace(),
    )


class TestStartupFailureIsRecordedOnAppState:
    def test_missing_backend_registry_records_the_reason(self) -> None:
        """The easiest real trigger of the production failure mode."""
        app = _make_app()

        result = _start(
            app,
            golden_repo_manager=_FakeGoldenRepoManager(_FakeActivatedRepoManager()),
            backend_registry=None,
        )

        assert result is None
        assert app.state.hnsw_orphan_repair_sweep_scheduler is None
        assert "backend_registry" in app.state.hnsw_orphan_repair_sweep_startup_error

    def test_missing_activated_repo_manager_records_the_reason(self) -> None:
        app = _make_app()

        result = _start(
            app,
            golden_repo_manager=_FakeGoldenRepoManager(None),
            backend_registry=SimpleNamespace(hnsw_orphan_sweep_state=SimpleNamespace()),
        )

        assert result is None
        assert (
            "activated_repo_manager" in app.state.hnsw_orphan_repair_sweep_startup_error
        )

    def test_failure_does_not_propagate_and_kill_boot(self) -> None:
        """Degrading is correct -- a fleet-repair subsystem must never take
        the whole node down at boot."""
        app = _make_app()

        # Must not raise.
        _start(
            app,
            golden_repo_manager=_FakeGoldenRepoManager(None),
            backend_registry=None,
        )


class TestStartupFailureIsLoggedAtErrorLevel:
    def test_failure_is_error_not_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A WARNING that scrolls past is what let this hide for two months.
        ERROR is the level ``admin_logs_query`` and the E2E log-audit gate
        actually select on."""
        app = _make_app()

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.startup.lifespan"
        ):
            _start(
                app,
                golden_repo_manager=_FakeGoldenRepoManager(_FakeActivatedRepoManager()),
                backend_registry=None,
            )

        sweep_records = [
            record
            for record in caplog.records
            if "APP-GENERAL-090" in record.getMessage()
        ]
        assert sweep_records, "startup failure was not logged at all"
        assert all(record.levelno == logging.ERROR for record in sweep_records), (
            "HNSW orphan sweep startup failure must be logged at ERROR, not "
            f"{[logging.getLevelName(r.levelno) for r in sweep_records]}"
        )


class TestSuccessfulStartupClearsTheError:
    def test_successful_start_wires_scheduler_and_nulls_the_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The success path must leave ``startup_error`` explicitly None, not
        merely absent -- the stats endpoint distinguishes 'no error' from
        'never reached this block'."""
        started: List[str] = []

        class _FakeScheduler:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

            def start(self) -> None:
                started.append("started")

        monkeypatch.setattr(
            "code_indexer.server.services.hnsw_orphan_sweep.scheduler."
            "HNSWOrphanRepairSweepScheduler",
            _FakeScheduler,
        )
        # Keep the test hermetic: the real get_config_service() constructs a
        # ConfigService against the live server data directory.
        monkeypatch.setattr(
            "code_indexer.server.services.config_service.get_config_service",
            lambda: SimpleNamespace(),
        )
        app = _make_app()

        result = _start(
            app,
            golden_repo_manager=_FakeGoldenRepoManager(_FakeActivatedRepoManager()),
            backend_registry=SimpleNamespace(hnsw_orphan_sweep_state=SimpleNamespace()),
        )

        assert started == ["started"]
        assert result is app.state.hnsw_orphan_repair_sweep_scheduler
        assert app.state.hnsw_orphan_repair_sweep_startup_error is None
