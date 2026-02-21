"""
Unit tests for Bug #240: Write mode lock leak on client disconnect.

Tests verify cleanup_stale_write_mode_markers() in RefreshScheduler:
- Stale markers older than TTL are removed
- Fresh markers younger than TTL are preserved
- The corresponding write lock is released when a marker is cleaned
- Corrupt JSON markers are removed gracefully (no crash)
- Missing .write_mode dir or empty dir does not crash
- Startup (force) cleanup removes ALL markers regardless of age
- cleanup is invoked in the scheduler loop (_scheduler_loop)
- Missing entered_at is treated as stale (remove it)

TDD RED phase: Tests written BEFORE production code changes. All tests are
expected to FAIL until the production code is implemented.
"""

import json
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Create a temporary golden-repos directory."""
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def mock_query_tracker():
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return config


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.list_global_repos.return_value = []
    registry.update_refresh_timestamp.return_value = None
    return registry


@pytest.fixture
def scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
):
    """Create RefreshScheduler with mock registry."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


def _write_marker(write_mode_dir: Path, alias: str, entered_at: datetime) -> Path:
    """Helper: create a .write_mode/{alias}.json marker file."""
    write_mode_dir.mkdir(parents=True, exist_ok=True)
    marker = write_mode_dir / f"{alias}.json"
    marker.write_text(
        json.dumps(
            {
                "alias": alias,
                "source_path": f"/some/path/{alias}",
                "entered_at": entered_at.isoformat(),
            },
            indent=2,
        )
    )
    return marker


# ---------------------------------------------------------------------------
# Test 1: Stale marker older than TTL is removed
# ---------------------------------------------------------------------------


class TestCleanupRemovesStaleMarker:
    """AC1: Stale write mode markers are detected and cleaned up."""

    def test_cleanup_removes_stale_marker(self, scheduler, golden_repos_dir):
        """
        A marker whose entered_at is older than WRITE_MODE_MARKER_TTL_SECONDS
        must be deleted by cleanup_stale_write_mode_markers().
        """
        write_mode_dir = golden_repos_dir / ".write_mode"
        # Create a marker that is 2 hours old (well past the 30-minute TTL)
        stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
        marker = _write_marker(write_mode_dir, "my-repo", stale_time)

        assert marker.exists(), "Marker must exist before cleanup"

        scheduler.cleanup_stale_write_mode_markers()

        assert not marker.exists(), (
            "Stale marker older than TTL must be deleted by cleanup"
        )


# ---------------------------------------------------------------------------
# Test 2: Fresh marker younger than TTL is NOT removed
# ---------------------------------------------------------------------------


class TestCleanupPreservesFreshMarker:
    """AC3: Cleanup mechanism does not remove markers for active sessions."""

    def test_cleanup_preserves_fresh_marker(self, scheduler, golden_repos_dir):
        """
        A marker whose entered_at is recent (within TTL) must NOT be deleted.
        """
        write_mode_dir = golden_repos_dir / ".write_mode"
        # Create a marker that is only 5 minutes old (well within the 30-minute TTL)
        fresh_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        marker = _write_marker(write_mode_dir, "active-repo", fresh_time)

        assert marker.exists(), "Marker must exist before cleanup"

        scheduler.cleanup_stale_write_mode_markers()

        assert marker.exists(), (
            "Fresh marker within TTL must NOT be removed by cleanup"
        )

        # Cleanup — remove marker manually so write lock doesn't remain
        marker.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Test 3: Lock is released when marker is cleaned
# ---------------------------------------------------------------------------


class TestCleanupReleasesCorrespondingWriteLock:
    """AC2: RefreshScheduler is not permanently blocked by orphaned markers."""

    def test_cleanup_releases_corresponding_write_lock(self, scheduler, golden_repos_dir):
        """
        When a stale marker is removed, release_write_lock() must be called
        with owner_name='mcp_write_mode' for that alias.
        """
        write_mode_dir = golden_repos_dir / ".write_mode"
        stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
        marker = _write_marker(write_mode_dir, "locked-repo", stale_time)

        # Simulate the lock being held (acquire it with mcp_write_mode owner)
        acquired = scheduler.acquire_write_lock("locked-repo", owner_name="mcp_write_mode")
        assert acquired, "Should be able to acquire write lock for test setup"

        # Verify lock IS held before cleanup
        assert scheduler.is_write_locked("locked-repo"), (
            "Write lock must be held before cleanup runs"
        )

        scheduler.cleanup_stale_write_mode_markers()

        # Marker must be gone
        assert not marker.exists(), "Stale marker must be removed"

        # Lock must be released — scheduler can now acquire it
        assert not scheduler.is_write_locked("locked-repo"), (
            "Write lock must be released after stale marker cleanup"
        )


# ---------------------------------------------------------------------------
# Test 4: Corrupt JSON marker is removed gracefully
# ---------------------------------------------------------------------------


class TestCleanupHandlesCorruptMarkerJson:
    """cleanup must be robust — corrupt JSON never crashes the scheduler."""

    def test_cleanup_handles_corrupt_marker_json(self, scheduler, golden_repos_dir):
        """
        A marker file containing invalid JSON must be removed without raising
        an exception. The scheduler must continue operating normally.
        """
        write_mode_dir = golden_repos_dir / ".write_mode"
        write_mode_dir.mkdir(parents=True, exist_ok=True)
        corrupt_marker = write_mode_dir / "corrupt-repo.json"
        corrupt_marker.write_text("THIS IS NOT VALID JSON {{{")

        # Must not raise
        try:
            scheduler.cleanup_stale_write_mode_markers()
        except Exception as exc:
            pytest.fail(
                f"cleanup_stale_write_mode_markers() must not raise on corrupt JSON, "
                f"but got {type(exc).__name__}: {exc}"
            )

        # Corrupt marker must be removed (treat as stale)
        assert not corrupt_marker.exists(), (
            "Corrupt JSON marker must be removed by cleanup"
        )


# ---------------------------------------------------------------------------
# Test 5: No .write_mode dir or empty dir is fine
# ---------------------------------------------------------------------------


class TestCleanupHandlesEmptyWriteModeDir:
    """cleanup must not crash when .write_mode dir is missing or empty."""

    def test_cleanup_handles_missing_write_mode_dir(self, scheduler, golden_repos_dir):
        """
        When golden_repos_dir/.write_mode/ does not exist, cleanup must
        complete without raising any exception.
        """
        write_mode_dir = golden_repos_dir / ".write_mode"
        assert not write_mode_dir.exists(), (
            "Test precondition: .write_mode dir must not exist"
        )

        try:
            scheduler.cleanup_stale_write_mode_markers()
        except Exception as exc:
            pytest.fail(
                f"cleanup_stale_write_mode_markers() must not raise when .write_mode "
                f"dir is missing, but got {type(exc).__name__}: {exc}"
            )

    def test_cleanup_handles_empty_write_mode_dir(self, scheduler, golden_repos_dir):
        """
        When golden_repos_dir/.write_mode/ exists but is empty, cleanup must
        complete without raising any exception.
        """
        write_mode_dir = golden_repos_dir / ".write_mode"
        write_mode_dir.mkdir(parents=True)

        try:
            scheduler.cleanup_stale_write_mode_markers()
        except Exception as exc:
            pytest.fail(
                f"cleanup_stale_write_mode_markers() must not raise for empty "
                f".write_mode dir, but got {type(exc).__name__}: {exc}"
            )


# ---------------------------------------------------------------------------
# Test 6: Startup cleanup removes ALL markers regardless of age
# ---------------------------------------------------------------------------


class TestCleanupOnStartupRemovesAllMarkers:
    """On startup ALL markers are stale because no MCP sessions survive restart."""

    def test_cleanup_on_startup_removes_all_markers(self, scheduler, golden_repos_dir):
        """
        cleanup_stale_write_mode_markers(force=True) must remove ALL markers
        regardless of their entered_at timestamp, because no MCP sessions
        survive a server restart.
        """
        write_mode_dir = golden_repos_dir / ".write_mode"

        # Create both a stale marker AND a fresh (recently-entered) marker
        stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
        fresh_time = datetime.now(timezone.utc) - timedelta(minutes=2)

        stale_marker = _write_marker(write_mode_dir, "old-repo", stale_time)
        fresh_marker = _write_marker(write_mode_dir, "new-repo", fresh_time)

        assert stale_marker.exists()
        assert fresh_marker.exists()

        # Force=True simulates startup: remove ALL markers unconditionally
        scheduler.cleanup_stale_write_mode_markers(force=True)

        assert not stale_marker.exists(), (
            "Stale marker must be removed on startup cleanup"
        )
        assert not fresh_marker.exists(), (
            "Fresh marker must ALSO be removed on startup cleanup (force=True)"
        )


# ---------------------------------------------------------------------------
# Test 7: Cleanup is called during the scheduler loop
# ---------------------------------------------------------------------------


class TestCleanupCalledDuringRefreshCycle:
    """cleanup must be invoked in _scheduler_loop so orphaned markers are
    periodically evicted even if the server runs for days."""

    def test_cleanup_called_during_scheduler_loop(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        _scheduler_loop must call cleanup_stale_write_mode_markers() on
        every iteration of the loop (or at least once per run).
        """
        cleanup_call_count = []

        original_cleanup = scheduler.cleanup_stale_write_mode_markers

        def tracking_cleanup(*args, **kwargs):
            cleanup_call_count.append(1)
            return original_cleanup(*args, **kwargs)

        # Make the loop run exactly one iteration then stop
        mock_registry.list_global_repos.return_value = []

        with patch.object(
            scheduler, "cleanup_stale_write_mode_markers", side_effect=tracking_cleanup
        ):
            # Run scheduler loop in a thread, stop after one iteration
            scheduler._running = True
            scheduler._stop_event.clear()

            loop_thread = threading.Thread(target=scheduler._scheduler_loop, daemon=True)
            loop_thread.start()

            # Give the loop one iteration to run
            time.sleep(0.1)
            scheduler._running = False
            scheduler._stop_event.set()
            loop_thread.join(timeout=2.0)

        assert len(cleanup_call_count) >= 1, (
            f"cleanup_stale_write_mode_markers() must be called at least once "
            f"during _scheduler_loop execution. Got {len(cleanup_call_count)} calls."
        )


# ---------------------------------------------------------------------------
# Test 8: Missing entered_at is treated as stale (remove it)
# ---------------------------------------------------------------------------


class TestCleanupRemovesMarkerWithoutEnteredAt:
    """Markers missing entered_at cannot be validated — treat them as stale."""

    def test_cleanup_removes_marker_without_entered_at(self, scheduler, golden_repos_dir):
        """
        A marker file that is valid JSON but lacks the 'entered_at' field
        must be treated as stale and removed (cannot validate its age).
        """
        write_mode_dir = golden_repos_dir / ".write_mode"
        write_mode_dir.mkdir(parents=True, exist_ok=True)
        no_timestamp_marker = write_mode_dir / "no-ts-repo.json"
        no_timestamp_marker.write_text(
            json.dumps(
                {
                    "alias": "no-ts-repo",
                    "source_path": "/some/path",
                    # Note: no 'entered_at' field
                },
                indent=2,
            )
        )

        scheduler.cleanup_stale_write_mode_markers()

        assert not no_timestamp_marker.exists(), (
            "Marker without 'entered_at' must be treated as stale and removed"
        )


# ---------------------------------------------------------------------------
# Test 9: Mixed stale, fresh, and corrupt markers
# ---------------------------------------------------------------------------


class TestCleanupMixedMarkers:
    """L3: Multiple markers where some are stale and some are fresh."""

    def test_cleanup_mixed_stale_fresh_corrupt_markers(self, scheduler, golden_repos_dir):
        """
        When .write_mode/ contains a mix of stale, fresh, and corrupt markers,
        non-force cleanup must:
        - Remove the stale marker (entered_at older than TTL)
        - Preserve the fresh marker (entered_at within TTL)
        - Remove the corrupt marker (invalid JSON)
        """
        write_mode_dir = golden_repos_dir / ".write_mode"
        write_mode_dir.mkdir(parents=True, exist_ok=True)

        # Marker 1: stale (2 hours old — well past 30-minute TTL)
        stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
        stale_marker = _write_marker(write_mode_dir, "stale-repo", stale_time)

        # Marker 2: fresh (5 minutes old — well within 30-minute TTL)
        fresh_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        fresh_marker = _write_marker(write_mode_dir, "fresh-repo", fresh_time)

        # Marker 3: corrupt (invalid JSON — cannot be parsed)
        corrupt_marker = write_mode_dir / "corrupt-mixed-repo.json"
        corrupt_marker.write_text("NOT VALID JSON }{{{")

        assert stale_marker.exists(), "Stale marker must exist before cleanup"
        assert fresh_marker.exists(), "Fresh marker must exist before cleanup"
        assert corrupt_marker.exists(), "Corrupt marker must exist before cleanup"

        # Non-force cleanup (periodic eviction)
        scheduler.cleanup_stale_write_mode_markers(force=False)

        assert not stale_marker.exists(), (
            "Stale marker (2h old) must be removed by non-force cleanup"
        )
        assert fresh_marker.exists(), (
            "Fresh marker (5min old) must be preserved by non-force cleanup"
        )
        assert not corrupt_marker.exists(), (
            "Corrupt JSON marker must be removed by non-force cleanup"
        )

        # Cleanup: remove the surviving fresh marker so no write locks linger
        fresh_marker.unlink(missing_ok=True)
