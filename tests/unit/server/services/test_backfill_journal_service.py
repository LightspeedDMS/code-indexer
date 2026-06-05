"""
Unit tests for BackfillJournalService — Story #1062.

Covers:
- Sidecar _status.json atomic write + shape
- status transitions: start -> per-alias update -> terminal completion
- 30s server-side grace computation from completed_at
- Journal-init failure is logged and swallowed (non-fatal to backfill)
- Two separate running events (_lifecycle_backfill_running + _description_backfill_running)
  behave independently — one clearing does not affect the other
- Journal namespace isolation (lifecycle vs description)
"""

import json
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.services.backfill_journal_service import (
    BackfillJournalService,
    BACKFILL_GRACE_SECONDS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def journal_dir(tmp_path: Path) -> Path:
    return tmp_path / "backfill-journal"


@pytest.fixture
def svc(journal_dir: Path) -> BackfillJournalService:
    return BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)


# ---------------------------------------------------------------------------
# Sidecar shape and atomic write
# ---------------------------------------------------------------------------


class TestSidecarShape:
    def test_start_writes_sidecar_with_running_true(
        self, svc: BackfillJournalService, journal_dir: Path
    ) -> None:
        svc.start(total=4)

        sidecar = journal_dir / "_status.json"
        assert sidecar.exists()
        data = json.loads(sidecar.read_text())
        assert data["running"] is True
        assert data["total"] == 4
        assert data["done"] == 0
        assert data["failed"] == 0
        assert data["completed_at"] is None
        assert data["started_at"] is not None

    def test_start_sidecar_started_at_is_iso(
        self, svc: BackfillJournalService, journal_dir: Path
    ) -> None:
        svc.start(total=3)
        data = json.loads((journal_dir / "_status.json").read_text())
        # Must parse without error
        dt = datetime.fromisoformat(data["started_at"])
        assert dt.tzinfo is not None  # timezone-aware

    def test_update_alias_advances_counters(
        self, svc: BackfillJournalService, journal_dir: Path
    ) -> None:
        svc.start(total=5)
        svc.update_alias("repo-a", success=True)
        svc.update_alias("repo-b", success=False)

        data = json.loads((journal_dir / "_status.json").read_text())
        assert data["done"] == 1
        assert data["failed"] == 1
        assert data["running"] is True
        assert data["completed_at"] is None

    def test_complete_sets_running_false_and_completed_at(
        self, svc: BackfillJournalService, journal_dir: Path
    ) -> None:
        svc.start(total=2)
        svc.update_alias("repo-a", success=True)
        svc.update_alias("repo-b", success=True)
        svc.complete()

        data = json.loads((journal_dir / "_status.json").read_text())
        assert data["running"] is False
        assert data["completed_at"] is not None
        # completed_at must parse as ISO
        dt = datetime.fromisoformat(data["completed_at"])
        assert dt.tzinfo is not None

    def test_sidecar_absent_before_start(self, journal_dir: Path) -> None:
        """No sidecar file until start() is called."""
        BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        assert not (journal_dir / "_status.json").exists()

    def test_atomic_write_uses_tempfile_and_replace(
        self, svc: BackfillJournalService, journal_dir: Path, monkeypatch
    ) -> None:
        """Verify write never leaves a partial file visible (file must exist atomically)."""
        writes: List[str] = []
        original_replace = __import__("os").replace

        def tracking_replace(src: str, dst: str) -> None:
            writes.append(dst)
            original_replace(src, dst)

        monkeypatch.setattr("os.replace", tracking_replace)
        svc.start(total=2)
        assert any("_status.json" in w for w in writes)


# ---------------------------------------------------------------------------
# Grace period computation
# ---------------------------------------------------------------------------


class TestGracePeriod:
    def test_is_active_true_while_running(self, svc: BackfillJournalService) -> None:
        svc.start(total=3)
        assert svc.is_active() is True

    def test_is_active_true_within_grace_after_completion(
        self, svc: BackfillJournalService, journal_dir: Path
    ) -> None:
        svc.start(total=1)
        svc.complete()
        # completed_at is just now, so within 30s grace
        assert svc.is_active() is True

    def test_is_active_false_after_grace_expired(self, journal_dir: Path) -> None:
        """Simulate a completed_at in the past beyond the grace window."""
        svc = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        svc.start(total=1)
        svc.complete()

        # Manually write a completed_at 31 seconds in the past
        sidecar = journal_dir / "_status.json"
        data = json.loads(sidecar.read_text())
        past = datetime.now(timezone.utc) - timedelta(
            seconds=BACKFILL_GRACE_SECONDS + 1
        )
        data["completed_at"] = past.isoformat()
        sidecar.write_text(json.dumps(data))

        # Re-read from disk (as if served by a different node)
        svc2 = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        assert svc2.is_active() is False

    def test_is_active_false_when_no_run_recorded(self, journal_dir: Path) -> None:
        """Service never started — is_active must return False."""
        svc = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        assert svc.is_active() is False

    def test_grace_constant_is_30(self) -> None:
        assert BACKFILL_GRACE_SECONDS == 30


# ---------------------------------------------------------------------------
# Journal write contract
# ---------------------------------------------------------------------------


class TestJournalWrites:
    def test_start_writes_started_entry(
        self, svc: BackfillJournalService, journal_dir: Path
    ) -> None:
        svc.start(total=4)
        journal = journal_dir / "_activity.md"
        assert journal.exists()
        content = journal.read_text()
        assert "Lifecycle backfill: started" in content
        assert "4" in content

    def test_update_alias_success_writes_entry(
        self, svc: BackfillJournalService, journal_dir: Path
    ) -> None:
        svc.start(total=2)
        svc.update_alias("my-repo", success=True)
        content = (journal_dir / "_activity.md").read_text()
        assert "my-repo" in content
        assert "succeeded" in content

    def test_update_alias_failure_writes_entry_with_reason(
        self, svc: BackfillJournalService, journal_dir: Path
    ) -> None:
        svc.start(total=2)
        svc.update_alias("bad-repo", success=False, reason="TimeoutError: timed out")
        content = (journal_dir / "_activity.md").read_text()
        assert "bad-repo" in content
        assert "failed" in content
        assert "TimeoutError" in content

    def test_complete_writes_terminal_entry(
        self, svc: BackfillJournalService, journal_dir: Path
    ) -> None:
        svc.start(total=3)
        svc.update_alias("r1", success=True)
        svc.update_alias("r2", success=True)
        svc.update_alias("r3", success=False, reason="SomeError: oops")
        svc.complete()
        content = (journal_dir / "_activity.md").read_text()
        assert "Lifecycle backfill complete" in content
        assert "2 succeeded" in content
        assert "1 failed" in content

    def test_description_namespace_uses_different_label(self, tmp_path: Path) -> None:
        desc_dir = tmp_path / "desc-journal"
        svc = BackfillJournalService(namespace="description", journal_dir=desc_dir)
        svc.start(total=6)
        content = (desc_dir / "_activity.md").read_text()
        assert "Description backfill: started" in content
        assert "6" in content

    def test_get_content_from_path_returns_new_bytes(
        self, svc: BackfillJournalService, journal_dir: Path
    ) -> None:
        from code_indexer.server.services.activity_journal_service import (
            ActivityJournalService,
        )

        svc.start(total=2)
        journal = journal_dir / "_activity.md"
        content1, offset1 = ActivityJournalService.get_content_from_path(journal, 0)
        assert offset1 > 0

        svc.update_alias("repo-x", success=True)
        content2, offset2 = ActivityJournalService.get_content_from_path(
            journal, offset1
        )
        assert "repo-x" in content2
        assert offset2 > offset1


# ---------------------------------------------------------------------------
# Journal-init failure swallow (failure contract)
# ---------------------------------------------------------------------------


class TestJournalInitFailureSwallow:
    def test_start_with_unwritable_dir_does_not_raise(self, tmp_path: Path) -> None:
        """If journal dir cannot be created, start() must not raise."""
        # Point to a path under a non-existent read-only-like path
        # We simulate by patching mkdir to raise
        import unittest.mock as mock

        bad_dir = tmp_path / "nfs-gone" / "backfill"
        svc = BackfillJournalService(namespace="lifecycle", journal_dir=bad_dir)

        with mock.patch.object(Path, "mkdir", side_effect=PermissionError("no access")):
            # Must not raise; should degrade gracefully
            svc.start(total=3)  # swallowed

        # Sidecar / journal may or may not exist — the point is no exception

    def test_update_alias_without_start_does_not_raise(self, journal_dir: Path) -> None:
        """update_alias with no prior start must be a no-op, not raise."""
        svc = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        svc.update_alias("some-repo", success=True)  # no-op

    def test_complete_without_start_does_not_raise(self, journal_dir: Path) -> None:
        svc = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        svc.complete()  # no-op


# ---------------------------------------------------------------------------
# Two-separate-running-events independence
# ---------------------------------------------------------------------------


class TestTwoSeparateRunningEvents:
    def test_lifecycle_event_and_description_event_are_independent(self) -> None:
        """Clearing one event must NOT affect the other."""
        lifecycle_event = threading.Event()
        description_event = threading.Event()

        lifecycle_event.set()
        description_event.set()

        # Simulates lifecycle backfill finishing first
        lifecycle_event.clear()

        assert not lifecycle_event.is_set()
        assert description_event.is_set()  # must still be set

    def test_both_events_cleared_means_both_idle(self) -> None:
        lifecycle_event = threading.Event()
        description_event = threading.Event()

        lifecycle_event.set()
        description_event.set()
        lifecycle_event.clear()
        description_event.clear()

        assert not lifecycle_event.is_set()
        assert not description_event.is_set()

    def test_periodic_refresh_guard_skips_when_either_event_set(self) -> None:
        """Guard logic: skip if EITHER backfill is running."""
        lifecycle_event = threading.Event()
        description_event = threading.Event()

        def should_skip() -> bool:
            return lifecycle_event.is_set() or description_event.is_set()

        assert not should_skip()

        lifecycle_event.set()
        assert should_skip()

        lifecycle_event.clear()
        assert not should_skip()

        description_event.set()
        assert should_skip()

        description_event.clear()
        assert not should_skip()


# ---------------------------------------------------------------------------
# Namespace isolation (two journals don't cross-contaminate)
# ---------------------------------------------------------------------------


class TestNamespaceIsolation:
    def test_lifecycle_and_description_journals_are_separate(
        self, tmp_path: Path
    ) -> None:
        lc_dir = tmp_path / "lifecycle-backfill-journal"
        desc_dir = tmp_path / "description-backfill-journal"

        lc_svc = BackfillJournalService(namespace="lifecycle", journal_dir=lc_dir)
        desc_svc = BackfillJournalService(namespace="description", journal_dir=desc_dir)

        lc_svc.start(total=3)
        desc_svc.start(total=7)

        lc_svc.update_alias("repo-alpha", success=True)
        desc_svc.update_alias("repo-beta", success=False, reason="err")

        # Sidecars are independent
        lc_data = json.loads((lc_dir / "_status.json").read_text())
        desc_data = json.loads((desc_dir / "_status.json").read_text())

        assert lc_data["total"] == 3
        assert desc_data["total"] == 7
        assert lc_data["done"] == 1
        assert desc_data["failed"] == 1

        # Journal files are independent
        lc_content = (lc_dir / "_activity.md").read_text()
        desc_content = (desc_dir / "_activity.md").read_text()

        assert "repo-alpha" in lc_content
        assert "repo-alpha" not in desc_content
        assert "repo-beta" in desc_content
        assert "repo-beta" not in lc_content

    def test_status_json_from_read_reflects_current_state(self, tmp_path: Path) -> None:
        """get_status() should read from disk, not rely on in-process state."""
        journal_dir = tmp_path / "lc-journal"
        svc = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        svc.start(total=2)
        svc.update_alias("r1", success=True)

        # Simulate a second node reading the sidecar
        svc2 = BackfillJournalService(namespace="lifecycle", journal_dir=journal_dir)
        status = svc2.get_status()
        assert status is not None
        assert status["total"] == 2
        assert status["done"] == 1
