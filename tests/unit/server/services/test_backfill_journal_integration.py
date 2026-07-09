"""
Integration tests for BackfillJournalService wired into DescriptionRefreshScheduler
— Story #1062.

Covers:
- Task 7: Two separate events (_lifecycle_backfill_running + _description_backfill_running)
  replace the single _backfill_in_progress event.
- Task 1: _run_lifecycle_backfill_async writes started/per-alias/terminal journal entries.
- Task 2: _run_description_backfill_async writes started/per-alias/terminal journal entries.
- Task 6: Routes GET /admin/partials/{lifecycle|description}-backfill-journal?offset=N.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCHEDULER_MODULE = "code_indexer.server.services.description_refresh_scheduler"


def _make_scheduler_bare(tmp_path: Path) -> Any:
    """
    Construct a DescriptionRefreshScheduler without calling __init__.

    Injects the minimal attributes needed, including the two new separate events
    and a golden_repos_dir pointing to tmp_path.
    """
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    sched = object.__new__(DescriptionRefreshScheduler)

    sched._lifecycle_invoker = MagicMock()
    sched._golden_repos_dir = tmp_path
    sched._lifecycle_debouncer = MagicMock()
    sched._refresh_scheduler = MagicMock(
        acquire_write_lock=MagicMock(return_value=True),
        release_write_lock=MagicMock(),
    )
    sched._job_tracker = MagicMock()
    sched._tracking_backend = MagicMock()
    sched._golden_backend = MagicMock()
    sched._config_manager = MagicMock()
    return sched


# ---------------------------------------------------------------------------
# Task 7: Two separate events
# ---------------------------------------------------------------------------


class TestTwoSeparateEvents:
    """Verify _lifecycle_backfill_running and _description_backfill_running exist."""

    def test_scheduler_has_lifecycle_backfill_running_event(
        self, tmp_path: Path
    ) -> None:
        """_lifecycle_backfill_running must exist and be a threading.Event."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        sched = object.__new__(DescriptionRefreshScheduler)
        # Minimal attrs to avoid AttributeError in constructor-bypassed object
        sched._lifecycle_invoker = None
        sched._golden_repos_dir = tmp_path
        sched._lifecycle_debouncer = None
        sched._refresh_scheduler = None
        sched._job_tracker = None
        sched._tracking_backend = MagicMock()
        sched._golden_backend = MagicMock()
        sched._config_manager = MagicMock()

        # The attribute must be set as part of __init__ — so we test the real
        # constructor via a known-valid minimal config.
        # Use the fact that tracking_backend+golden_backend bypass db_path requirement.
        real_sched = DescriptionRefreshScheduler(
            tracking_backend=MagicMock(),
            golden_backend=MagicMock(),
            config_manager=MagicMock(load_config=lambda: None),
        )
        assert hasattr(real_sched, "_lifecycle_backfill_running"), (
            "_lifecycle_backfill_running event missing from scheduler"
        )
        assert isinstance(real_sched._lifecycle_backfill_running, threading.Event), (
            "_lifecycle_backfill_running must be threading.Event"
        )

    def test_scheduler_has_description_backfill_running_event(self) -> None:
        """_description_backfill_running must exist and be a threading.Event."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        real_sched = DescriptionRefreshScheduler(
            tracking_backend=MagicMock(),
            golden_backend=MagicMock(),
            config_manager=MagicMock(load_config=lambda: None),
        )
        assert hasattr(real_sched, "_description_backfill_running"), (
            "_description_backfill_running event missing from scheduler"
        )
        assert isinstance(real_sched._description_backfill_running, threading.Event), (
            "_description_backfill_running must be threading.Event"
        )

    def test_old_backfill_in_progress_replaced(self) -> None:
        """_backfill_in_progress must NOT exist — it has been replaced by two events."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        real_sched = DescriptionRefreshScheduler(
            tracking_backend=MagicMock(),
            golden_backend=MagicMock(),
            config_manager=MagicMock(load_config=lambda: None),
        )
        assert not hasattr(real_sched, "_backfill_in_progress"), (
            "_backfill_in_progress still present — it must be removed and replaced "
            "by _lifecycle_backfill_running + _description_backfill_running"
        )

    def test_lifecycle_event_set_does_not_affect_description_event(self) -> None:
        """Clearing lifecycle event must not affect description event."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        real_sched = DescriptionRefreshScheduler(
            tracking_backend=MagicMock(),
            golden_backend=MagicMock(),
            config_manager=MagicMock(load_config=lambda: None),
        )
        real_sched._lifecycle_backfill_running.set()
        real_sched._description_backfill_running.set()

        # Simulate lifecycle backfill finishing first
        real_sched._lifecycle_backfill_running.clear()

        assert not real_sched._lifecycle_backfill_running.is_set()
        assert real_sched._description_backfill_running.is_set(), (
            "clearing lifecycle event must not clear description event"
        )

    def test_periodic_refresh_guard_checks_both_events(self) -> None:
        """_run_loop_single_pass must check BOTH events — skip if EITHER is set."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        real_sched = DescriptionRefreshScheduler(
            tracking_backend=MagicMock(),
            golden_backend=MagicMock(),
            config_manager=MagicMock(load_config=lambda: None),
        )
        # Patch get_stale_repos to track if it was called
        real_sched.get_stale_repos = MagicMock(return_value=[])

        # Neither running — should NOT skip
        real_sched._lifecycle_backfill_running.clear()
        real_sched._description_backfill_running.clear()
        real_sched._run_loop_single_pass()
        real_sched.get_stale_repos.assert_called_once()

        real_sched.get_stale_repos.reset_mock()

        # Only lifecycle running — should skip
        real_sched._lifecycle_backfill_running.set()
        real_sched._description_backfill_running.clear()
        real_sched._run_loop_single_pass()
        real_sched.get_stale_repos.assert_not_called()

        real_sched.get_stale_repos.reset_mock()
        real_sched._lifecycle_backfill_running.clear()

        # Only description running — should also skip
        real_sched._lifecycle_backfill_running.clear()
        real_sched._description_backfill_running.set()
        real_sched._run_loop_single_pass()
        real_sched.get_stale_repos.assert_not_called()


# ---------------------------------------------------------------------------
# Task 1: _run_lifecycle_backfill_async journal integration
# ---------------------------------------------------------------------------


class TestLifecycleBackfillAsyncJournal:
    """Verify _run_lifecycle_backfill_async writes journal entries via BackfillJournalService."""

    def test_lifecycle_backfill_async_creates_journal_and_status(
        self, tmp_path: Path
    ) -> None:
        """After _run_lifecycle_backfill_async completes, lifecycle _status.json exists."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        # Set up golden_repos_dir and scratch dir
        golden_dir = tmp_path / "golden-repos"
        golden_dir.mkdir()
        scratch_dir = golden_dir / ".scratch"
        scratch_dir.mkdir()

        real_sched = DescriptionRefreshScheduler(
            tracking_backend=MagicMock(),
            golden_backend=MagicMock(),
            config_manager=MagicMock(load_config=lambda: None),
        )
        real_sched._golden_repos_dir = golden_dir

        # Wire collaborators
        real_sched._job_tracker = MagicMock()
        real_sched._refresh_scheduler = MagicMock(
            acquire_write_lock=MagicMock(return_value=True),
            release_write_lock=MagicMock(),
        )
        real_sched._lifecycle_debouncer = MagicMock()
        real_sched._tracking_backend = MagicMock()

        # Use a fast invoker that returns a trivial result

        mock_invoker = MagicMock(
            return_value=MagicMock(lifecycle={"confidence": "high"}, description="desc")
        )
        real_sched._lifecycle_invoker = mock_invoker

        # Run with a single alias
        aliases = ["repo-alpha"]
        real_sched._run_lifecycle_backfill_async(aliases)

        # Journal dir must exist
        journal_dir = golden_dir / ".scratch" / "lifecycle-backfill-journal"
        assert journal_dir.exists(), "lifecycle-backfill-journal dir must be created"

        # _status.json must exist and have running=False (completed)
        sidecar = journal_dir / "_status.json"
        assert sidecar.exists(), "_status.json sidecar must be written"
        data = json.loads(sidecar.read_text())
        assert data["running"] is False
        assert data["completed_at"] is not None
        assert data["total"] == 1

    def test_lifecycle_backfill_async_journal_has_started_entry(
        self, tmp_path: Path
    ) -> None:
        """_activity.md must contain a 'started' entry for the lifecycle backfill."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        golden_dir = tmp_path / "golden-repos"
        golden_dir.mkdir()

        real_sched = DescriptionRefreshScheduler(
            tracking_backend=MagicMock(),
            golden_backend=MagicMock(),
            config_manager=MagicMock(load_config=lambda: None),
        )
        real_sched._golden_repos_dir = golden_dir
        real_sched._job_tracker = MagicMock()
        real_sched._refresh_scheduler = MagicMock(
            acquire_write_lock=MagicMock(return_value=True),
            release_write_lock=MagicMock(),
        )
        real_sched._lifecycle_debouncer = MagicMock()
        real_sched._tracking_backend = MagicMock()
        real_sched._lifecycle_invoker = MagicMock(
            return_value=MagicMock(lifecycle={"confidence": "high"}, description="d")
        )

        real_sched._run_lifecycle_backfill_async(["repo-beta"])

        journal_dir = golden_dir / ".scratch" / "lifecycle-backfill-journal"
        journal = journal_dir / "_activity.md"
        assert journal.exists()
        content = journal.read_text()
        assert "Lifecycle backfill: started" in content
        assert "1" in content

    def test_lifecycle_backfill_async_sets_and_clears_its_own_event(
        self, tmp_path: Path
    ) -> None:
        """_lifecycle_backfill_running must be set during run and cleared after."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        golden_dir = tmp_path / "golden-repos"
        golden_dir.mkdir()

        real_sched = DescriptionRefreshScheduler(
            tracking_backend=MagicMock(),
            golden_backend=MagicMock(),
            config_manager=MagicMock(load_config=lambda: None),
        )
        real_sched._golden_repos_dir = golden_dir
        real_sched._job_tracker = MagicMock()
        real_sched._refresh_scheduler = MagicMock(
            acquire_write_lock=MagicMock(return_value=True),
            release_write_lock=MagicMock(),
        )
        real_sched._lifecycle_debouncer = MagicMock()
        real_sched._tracking_backend = MagicMock()
        real_sched._lifecycle_invoker = MagicMock(
            return_value=MagicMock(lifecycle={"confidence": "high"}, description="d")
        )

        assert not real_sched._lifecycle_backfill_running.is_set()
        real_sched._run_lifecycle_backfill_async(["repo-gamma"])
        # After completion, event must be cleared
        assert not real_sched._lifecycle_backfill_running.is_set()


# ---------------------------------------------------------------------------
# Task 2: _run_description_backfill_async journal integration
# ---------------------------------------------------------------------------


class TestDescriptionBackfillAsyncJournal:
    """Verify _run_description_backfill_async writes journal entries via BackfillJournalService."""

    def test_description_backfill_async_creates_journal_and_status(
        self, tmp_path: Path
    ) -> None:
        """After _run_description_backfill_async completes, description _status.json exists."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        golden_dir = tmp_path / "golden-repos"
        golden_dir.mkdir()

        real_sched = DescriptionRefreshScheduler(
            tracking_backend=MagicMock(),
            golden_backend=MagicMock(),
            config_manager=MagicMock(load_config=lambda: None),
        )
        real_sched._golden_repos_dir = golden_dir
        real_sched._job_tracker = MagicMock()
        real_sched._refresh_scheduler = MagicMock(
            acquire_write_lock=MagicMock(return_value=True),
            release_write_lock=MagicMock(),
        )
        real_sched._lifecycle_debouncer = MagicMock()
        real_sched._tracking_backend = MagicMock()
        real_sched._lifecycle_invoker = MagicMock(
            return_value=MagicMock(lifecycle={"confidence": "high"}, description="d")
        )

        real_sched._run_description_backfill_async(["repo-delta"])

        journal_dir = golden_dir / ".scratch" / "description-backfill-journal"
        assert journal_dir.exists(), "description-backfill-journal dir must be created"

        sidecar = journal_dir / "_status.json"
        assert sidecar.exists(), "_status.json sidecar must be written"
        data = json.loads(sidecar.read_text())
        assert data["running"] is False
        assert data["completed_at"] is not None
        assert data["total"] == 1

    def test_description_backfill_async_journal_has_started_entry(
        self, tmp_path: Path
    ) -> None:
        """_activity.md must contain 'Description backfill: started' entry."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        golden_dir = tmp_path / "golden-repos"
        golden_dir.mkdir()

        real_sched = DescriptionRefreshScheduler(
            tracking_backend=MagicMock(),
            golden_backend=MagicMock(),
            config_manager=MagicMock(load_config=lambda: None),
        )
        real_sched._golden_repos_dir = golden_dir
        real_sched._job_tracker = MagicMock()
        real_sched._refresh_scheduler = MagicMock(
            acquire_write_lock=MagicMock(return_value=True),
            release_write_lock=MagicMock(),
        )
        real_sched._lifecycle_debouncer = MagicMock()
        real_sched._tracking_backend = MagicMock()
        real_sched._lifecycle_invoker = MagicMock(
            return_value=MagicMock(lifecycle={"confidence": "high"}, description="d")
        )

        real_sched._run_description_backfill_async(["repo-epsilon", "repo-zeta"])

        journal_dir = golden_dir / ".scratch" / "description-backfill-journal"
        journal = journal_dir / "_activity.md"
        assert journal.exists()
        content = journal.read_text()
        assert "Description backfill: started" in content
        assert "2" in content

    def test_description_backfill_async_sets_and_clears_its_own_event(
        self, tmp_path: Path
    ) -> None:
        """_description_backfill_running must be set during run and cleared after."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        golden_dir = tmp_path / "golden-repos"
        golden_dir.mkdir()

        real_sched = DescriptionRefreshScheduler(
            tracking_backend=MagicMock(),
            golden_backend=MagicMock(),
            config_manager=MagicMock(load_config=lambda: None),
        )
        real_sched._golden_repos_dir = golden_dir
        real_sched._job_tracker = MagicMock()
        real_sched._refresh_scheduler = MagicMock(
            acquire_write_lock=MagicMock(return_value=True),
            release_write_lock=MagicMock(),
        )
        real_sched._lifecycle_debouncer = MagicMock()
        real_sched._tracking_backend = MagicMock()
        real_sched._lifecycle_invoker = MagicMock(
            return_value=MagicMock(lifecycle={"confidence": "high"}, description="d")
        )

        assert not real_sched._description_backfill_running.is_set()
        real_sched._run_description_backfill_async(["repo-eta"])
        assert not real_sched._description_backfill_running.is_set()

    def test_two_journals_are_independent_per_namespace(self, tmp_path: Path) -> None:
        """Lifecycle and description journals live in separate directories."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        golden_dir = tmp_path / "golden-repos"
        golden_dir.mkdir()

        real_sched = DescriptionRefreshScheduler(
            tracking_backend=MagicMock(),
            golden_backend=MagicMock(),
            config_manager=MagicMock(load_config=lambda: None),
        )
        real_sched._golden_repos_dir = golden_dir
        real_sched._job_tracker = MagicMock()
        real_sched._refresh_scheduler = MagicMock(
            acquire_write_lock=MagicMock(return_value=True),
            release_write_lock=MagicMock(),
        )
        real_sched._lifecycle_debouncer = MagicMock()
        real_sched._tracking_backend = MagicMock()
        real_sched._lifecycle_invoker = MagicMock(
            return_value=MagicMock(lifecycle={"confidence": "high"}, description="d")
        )

        real_sched._run_lifecycle_backfill_async(["lc-repo"])
        real_sched._run_description_backfill_async(["desc-repo"])

        lc_journal = (
            golden_dir / ".scratch" / "lifecycle-backfill-journal" / "_activity.md"
        )
        desc_journal = (
            golden_dir / ".scratch" / "description-backfill-journal" / "_activity.md"
        )

        lc_content = lc_journal.read_text()
        desc_content = desc_journal.read_text()

        # Lifecycle journal should NOT contain description entries and vice versa
        assert "Lifecycle backfill" in lc_content
        assert "Description backfill" not in lc_content
        assert "Description backfill" in desc_content
        assert "Lifecycle backfill" not in desc_content

        # Sidecars are independent
        lc_status = json.loads(
            (
                golden_dir / ".scratch" / "lifecycle-backfill-journal" / "_status.json"
            ).read_text()
        )
        desc_status = json.loads(
            (
                golden_dir
                / ".scratch"
                / "description-backfill-journal"
                / "_status.json"
            ).read_text()
        )
        assert lc_status["total"] == 1
        assert desc_status["total"] == 1


# ---------------------------------------------------------------------------
# Task 6: Route header contract tests (unit-level, no FastAPI TestClient)
# ---------------------------------------------------------------------------


class TestBackfillJournalRouteHeaders:
    """Unit-level tests for the header contract of the backfill journal routes.

    These tests verify the _build_backfill_journal_response() helper (or equivalent)
    populates headers correctly from a BackfillJournalService sidecar.
    """

    def test_route_header_active_1_when_running(self, tmp_path: Path) -> None:
        """When sidecar running=True, response header X-Backfill-Active must be '1'."""
        from code_indexer.server.services.backfill_journal_service import (
            BackfillJournalService,
        )
        from code_indexer.server.web.dependency_map_routes import (
            _backfill_journal_headers_from_service,
        )

        journal_dir = tmp_path / "lifecycle-backfill-journal"
        svc = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        svc.start(total=3)

        headers = _backfill_journal_headers_from_service(svc, offset=0)
        assert headers["X-Backfill-Active"] == "1"
        assert headers["X-Backfill-Total"] == "3"
        assert headers["X-Backfill-Done"] == "0"
        assert headers["X-Backfill-Failed"] == "0"
        assert headers["X-Backfill-Completed-At"] == ""

    def test_route_header_active_0_after_grace(self, tmp_path: Path) -> None:
        """When sidecar running=False and completed_at > 30s ago, X-Backfill-Active='0'."""
        from code_indexer.server.services.backfill_journal_service import (
            BackfillJournalService,
            BACKFILL_GRACE_SECONDS,
        )
        from code_indexer.server.web.dependency_map_routes import (
            _backfill_journal_headers_from_service,
        )

        journal_dir = tmp_path / "lc-journal"
        svc = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        svc.start(total=2)
        svc.complete()

        # Manually backdate completed_at beyond grace window
        sidecar = journal_dir / "_status.json"
        data = json.loads(sidecar.read_text())
        past = datetime.now(timezone.utc) - timedelta(
            seconds=BACKFILL_GRACE_SECONDS + 5
        )
        data["completed_at"] = past.isoformat()
        data["running"] = False
        sidecar.write_text(json.dumps(data))

        headers = _backfill_journal_headers_from_service(svc, offset=0)
        assert headers["X-Backfill-Active"] == "0"

    def test_route_header_active_1_within_grace(self, tmp_path: Path) -> None:
        """When completed_at is recent (within grace), X-Backfill-Active must be '1'."""
        from code_indexer.server.services.backfill_journal_service import (
            BackfillJournalService,
        )
        from code_indexer.server.web.dependency_map_routes import (
            _backfill_journal_headers_from_service,
        )

        journal_dir = tmp_path / "lc-journal2"
        svc = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        svc.start(total=1)
        svc.complete()

        # completed_at is just now — within grace
        headers = _backfill_journal_headers_from_service(svc, offset=0)
        assert headers["X-Backfill-Active"] == "1"

    def test_route_header_active_0_when_never_started(self, tmp_path: Path) -> None:
        """When no sidecar exists (never started), X-Backfill-Active must be '0'."""
        from code_indexer.server.services.backfill_journal_service import (
            BackfillJournalService,
        )
        from code_indexer.server.web.dependency_map_routes import (
            _backfill_journal_headers_from_service,
        )

        journal_dir = tmp_path / "lc-journal3"
        svc = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        # Never started

        headers = _backfill_journal_headers_from_service(svc, offset=0)
        assert headers["X-Backfill-Active"] == "0"
        assert headers["X-Backfill-Total"] == "0"

    def test_route_header_offset_advances_on_new_content(self, tmp_path: Path) -> None:
        """X-Journal-Offset must return a non-zero value when journal has content."""
        from code_indexer.server.services.backfill_journal_service import (
            BackfillJournalService,
        )
        from code_indexer.server.web.dependency_map_routes import (
            _backfill_journal_headers_from_service,
        )

        journal_dir = tmp_path / "lc-journal4"
        svc = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        svc.start(total=2)
        svc.update_alias("repo-a", success=True)

        headers = _backfill_journal_headers_from_service(svc, offset=0)
        new_offset = int(headers["X-Journal-Offset"])
        assert new_offset > 0, "Journal-Offset must advance when journal has content"

    def test_route_header_completed_at_present_after_completion(
        self, tmp_path: Path
    ) -> None:
        """X-Backfill-Completed-At must be a non-empty ISO string after completion."""
        from code_indexer.server.services.backfill_journal_service import (
            BackfillJournalService,
        )
        from code_indexer.server.web.dependency_map_routes import (
            _backfill_journal_headers_from_service,
        )

        journal_dir = tmp_path / "lc-journal5"
        svc = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        svc.start(total=1)
        svc.update_alias("repo-x", success=True)
        svc.complete()

        headers = _backfill_journal_headers_from_service(svc, offset=0)
        completed_at = headers["X-Backfill-Completed-At"]
        assert completed_at != "", (
            "X-Backfill-Completed-At must be set after completion"
        )
        # Must be parseable ISO
        dt = datetime.fromisoformat(completed_at)
        assert dt.tzinfo is not None
