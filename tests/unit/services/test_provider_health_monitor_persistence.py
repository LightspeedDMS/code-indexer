"""Tests for ProviderHealthMonitor file-backed persistence (Story #691).

Tests the optional persistence_path ctor parameter extension.
Uses real filesystem (tmp_path), real multiprocessing for concurrency tests.
No mocks of file I/O — anti-mock (Messi Rule 01).

Flock behavioral test design:
  The writer thread sets `about_to_sinbin` immediately before calling
  `m.sinbin()`. Construction only reads the file (no lock taken); so once
  `about_to_sinbin` is set, the very next file operation is the LOCK_EX
  acquire inside `_persist_to_file`. This gives the main process a
  deterministic signal to release the child's lock without any sleeps.
"""

import fcntl
import json
import logging
import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from code_indexer.services.provider_health_monitor import ProviderHealthMonitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_monitor(path: Path) -> ProviderHealthMonitor:
    """Return a new ProviderHealthMonitor instance with persistence_path set."""
    return ProviderHealthMonitor(persistence_path=path)


def _read_state_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Module-level helpers (must be picklable for multiprocessing)
# ---------------------------------------------------------------------------


def _lock_holder(
    path_str: str,
    ready_event: multiprocessing.Event,  # type: ignore[type-arg]  # multiprocessing.Event is generic only in 3.9+
    release_event: multiprocessing.Event,  # type: ignore[type-arg]
) -> None:
    """Child process: acquire LOCK_EX on path, signal ready, wait for release."""
    path = Path(path_str)
    path.write_text("{}", encoding="utf-8")
    fd = open(path, "r+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ready_event.set()
        release_event.wait(timeout=10)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        fd.close()


def _worker_sinbin(path_str: str, provider: str) -> None:
    """Sinbin a provider multiple times. Used in multiprocessing concurrency tests."""
    path = Path(path_str)
    m = ProviderHealthMonitor(persistence_path=path)
    for _ in range(5):
        m.sinbin(provider)


# ---------------------------------------------------------------------------
# Scenario: Server default behavior unchanged (regression guard)
# ---------------------------------------------------------------------------


class TestServerDefaultUnchanged:
    """ProviderHealthMonitor without persistence_path behaves identically to today."""

    def test_no_persistence_path_no_file_created(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        m = ProviderHealthMonitor()
        m.sinbin("voyage-reranker")
        assert not path.exists(), "No file should be created for in-memory monitor"

    def test_in_memory_sinbin_still_works(self) -> None:
        m = ProviderHealthMonitor()
        m.sinbin("voyage-reranker")
        assert m.is_sinbinned("voyage-reranker")


# ---------------------------------------------------------------------------
# Scenario: File created on first sinbin
# ---------------------------------------------------------------------------


class TestFileCreatedOnSinbin:
    """When persistence_path set, sinbin state is written to file."""

    def test_file_created_after_sinbin(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        m = _fresh_monitor(path)
        assert not path.exists()
        m.sinbin("voyage-reranker")
        assert path.exists()

    def test_file_contains_wall_clock_timestamp(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        m = _fresh_monitor(path)
        before_wall = time.time()
        m.sinbin("voyage-reranker")
        after_wall = time.time()
        state = _read_state_file(path)
        expiry = state["voyage-reranker"]["sinbin_until_wall_seconds"]
        assert isinstance(expiry, float)
        assert expiry > before_wall
        assert expiry < after_wall + 3600

    def test_file_is_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        m = _fresh_monitor(path)
        m.sinbin("cohere-reranker")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# Scenario: Reload across instances preserves sinbin state
# ---------------------------------------------------------------------------


class TestReloadPreservesSinbin:
    """A new monitor instance reading from the same file sees sinbin state."""

    def test_sinbin_visible_to_second_monitor_instance(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        m1 = _fresh_monitor(path)
        m1.sinbin("voyage-reranker")
        m2 = _fresh_monitor(path)
        assert m2.is_sinbinned("voyage-reranker"), (
            "Second monitor instance must see sinbin state loaded from file"
        )

    def test_non_sinbinned_provider_not_visible_as_sinbinned(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "state.json"
        m1 = _fresh_monitor(path)
        m1.sinbin("voyage-reranker")
        m2 = _fresh_monitor(path)
        assert not m2.is_sinbinned("cohere-reranker")


# ---------------------------------------------------------------------------
# Scenario: Sin-bin expiry clears on load
# ---------------------------------------------------------------------------


class TestSinbinExpiryClearsOnLoad:
    """Expired sinbin timestamps are not active after reload."""

    def test_past_expiry_not_sinbinned_after_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        past_expiry = time.time() - 10.0
        state = {
            "voyage-reranker": {
                "sinbin_until_wall_seconds": past_expiry,
                "last_failure_kind": "timeout",
            }
        }
        path.write_text(json.dumps(state), encoding="utf-8")
        m = _fresh_monitor(path)
        assert not m.is_sinbinned("voyage-reranker"), (
            "Expired sinbin must not be active after loading from file"
        )

    def test_future_expiry_still_sinbinned_after_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        future_expiry = time.time() + 300.0
        state = {
            "voyage-reranker": {
                "sinbin_until_wall_seconds": future_expiry,
                "last_failure_kind": "timeout",
            }
        }
        path.write_text(json.dumps(state), encoding="utf-8")
        m = _fresh_monitor(path)
        assert m.is_sinbinned("voyage-reranker")


# ---------------------------------------------------------------------------
# Scenario: Missing persistence file on first load
# ---------------------------------------------------------------------------


class TestMissingFileOnFirstLoad:
    """A missing persistence file results in empty state — no error."""

    def test_missing_file_yields_empty_state(self, tmp_path: Path) -> None:
        path = tmp_path / "nonexistent.json"
        m = _fresh_monitor(path)
        assert not m.is_sinbinned("voyage-reranker")
        assert not m.is_sinbinned("cohere-reranker")


# ---------------------------------------------------------------------------
# Scenario: Corrupted persistence file
# ---------------------------------------------------------------------------


class TestCorruptedPersistenceFile:
    """Corrupted file yields empty state and logs a warning."""

    def test_corrupted_file_yields_empty_state(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "state.json"
        path.write_text("{not valid json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            m = _fresh_monitor(path)
        assert not m.is_sinbinned("voyage-reranker")

    def test_corrupted_file_logs_warning_with_path(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "state.json"
        path.write_text("{", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            _fresh_monitor(path)
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(str(path) in r.message for r in warnings), (
            f"Expected warning mentioning {path}, got: {[r.message for r in warnings]}"
        )

    def test_corrupted_file_overwritten_on_next_write(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("{bad", encoding="utf-8")
        m = _fresh_monitor(path)
        m.sinbin("voyage-reranker")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# Scenario: flock is used during write (deterministic behavioral test)
# ---------------------------------------------------------------------------


class TestFlockUsed:
    """Verify flock semantics via real lock-contention with deterministic synchronization.

    Synchronization design:
      - The writer thread sets `about_to_sinbin` immediately before calling
        `m.sinbin()`. Construction only reads the already-present `{}` file
        (no LOCK_EX taken in __init__). So once `about_to_sinbin` fires, the
        next file I/O is the LOCK_EX acquisition inside `_persist_to_file()`.
      - This gives the main thread a deterministic signal to release the child
        lock without any sleeps.
    """

    def test_monitor_write_completes_after_lock_released(self, tmp_path: Path) -> None:
        """Child holds LOCK_EX; writer signals just before sinbin; completes after release.

        Steps:
          1. Child process creates state file with `{}`, acquires LOCK_EX, signals ready.
          2. Main constructs monitor (reads `{}` — no lock taken during __init__).
          3. Writer thread sets `about_to_sinbin` then calls `m.sinbin()`.
          4. Main waits on `about_to_sinbin` (deterministic).
          5. Main signals release_event; child releases LOCK_EX.
          6. Assert write_done fires and state contains the provider.
          7. Assert child exited cleanly.
        """
        path = tmp_path / "state.json"
        # Use multiprocessing.Event; type-arg syntax requires Python 3.9+, so
        # we use the runtime factory form which is compatible with 3.8+.
        mp_ctx = multiprocessing.get_context("fork")
        ready_event = mp_ctx.Event()
        release_event = mp_ctx.Event()

        child = mp_ctx.Process(
            target=_lock_holder,
            args=(str(path), ready_event, release_event),
        )
        child.start()
        try:
            assert ready_event.wait(timeout=5), "Child did not acquire lock in time"

            # Construct monitor now — __init__ reads the `{}` file, no LOCK_EX taken.
            m = _fresh_monitor(path)

            about_to_sinbin = threading.Event()
            write_done = threading.Event()
            write_error: list = []

            def do_sinbin() -> None:
                try:
                    # Signal immediately before sinbin; next file op = LOCK_EX in persist.
                    about_to_sinbin.set()
                    m.sinbin("voyage-reranker")
                    write_done.set()
                except Exception as exc:
                    write_error.append(exc)
                    write_done.set()

            t = threading.Thread(target=do_sinbin, daemon=True)
            t.start()

            # Wait deterministically for writer to reach the sinbin call boundary.
            assert about_to_sinbin.wait(timeout=5), (
                "Writer thread did not signal about_to_sinbin within 5s"
            )

            # Writer is at (or just past) the sinbin call — release child LOCK_EX.
            release_event.set()

            completed = write_done.wait(timeout=5)
            assert completed, (
                "Monitor write did not complete within 5s after lock release"
            )
            assert not write_error, f"Monitor write raised: {write_error[0]}"

            data = json.loads(path.read_text(encoding="utf-8"))
            assert "voyage-reranker" in data, (
                "voyage-reranker must be persisted after write completed"
            )
        finally:
            release_event.set()  # safety: never leave child waiting
            child.join(timeout=5)
        assert child.exitcode == 0, (
            f"Lock-holder child exited with {child.exitcode} (expected 0)"
        )


# ---------------------------------------------------------------------------
# Scenario: Multi-process concurrent writes do not corrupt file
# ---------------------------------------------------------------------------


class TestConcurrentWrites:
    """Two monitors with the same path can write concurrently without corruption."""

    def test_concurrent_sinbin_does_not_corrupt_file(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        p1 = multiprocessing.Process(
            target=_worker_sinbin, args=(str(path), "voyage-reranker")
        )
        p2 = multiprocessing.Process(
            target=_worker_sinbin, args=(str(path), "cohere-reranker")
        )
        p1.start()
        p2.start()
        p1.join(timeout=15)
        p2.join(timeout=15)
        assert p1.exitcode == 0, f"Worker 1 exited with {p1.exitcode}"
        assert p2.exitcode == 0, f"Worker 2 exited with {p2.exitcode}"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "voyage-reranker" in data, (
            "voyage-reranker sinbin must be persisted after concurrent writes"
        )
        assert "cohere-reranker" in data, (
            "cohere-reranker sinbin must be persisted after concurrent writes"
        )
        for provider in ("voyage-reranker", "cohere-reranker"):
            expiry = data[provider]["sinbin_until_wall_seconds"]
            assert isinstance(expiry, float) and expiry > time.time(), (
                f"{provider} sinbin_until_wall_seconds must be in the future"
            )


# ---------------------------------------------------------------------------
# Scenario: clear_sinbin removes persisted state (BLOCKER 1 regression test)
# ---------------------------------------------------------------------------


class TestClearSinbinPersistence:
    """clear_sinbin must remove the provider from the persistence file so that
    a new CLI invocation does not reload a stale sinbin entry."""

    def test_clear_sinbin_removes_persisted_entry(self, tmp_path: Path) -> None:
        """sinbin -> clear_sinbin -> reload -> is_sinbinned() must return False.

        Repro scenario from codex review: without the fix, _build_merged_state
        starts from dict(existing) and re-merges the stale file entry back in,
        so the next CLI invocation still sees the provider as sinbinned.
        """
        path = tmp_path / "state.json"
        m1 = _fresh_monitor(path)
        m1.sinbin("voyage-reranker")
        assert m1.is_sinbinned("voyage-reranker"), "Pre-condition: must be sinbinned"
        assert path.exists(), "Pre-condition: file must exist after sinbin"

        m1.clear_sinbin("voyage-reranker")

        # The file must no longer contain the cleared provider
        state = _read_state_file(path)
        assert "voyage-reranker" not in state, (
            "clear_sinbin must remove the provider from the persistence file; "
            f"got file contents: {state}"
        )

        # A new instance loading from the same file must not see the provider as sinbinned
        m2 = _fresh_monitor(path)
        assert not m2.is_sinbinned("voyage-reranker"), (
            "New monitor instance must not reload stale sinbin after clear_sinbin"
        )

    def test_clear_sinbin_leaves_other_providers_intact(self, tmp_path: Path) -> None:
        """Clearing one provider must not remove others from the persistence file."""
        path = tmp_path / "state.json"
        m = _fresh_monitor(path)
        m.sinbin("voyage-reranker")
        m.sinbin("cohere-reranker")
        m.clear_sinbin("voyage-reranker")

        state = _read_state_file(path)
        assert "voyage-reranker" not in state, (
            "Cleared provider must be absent from file"
        )
        assert "cohere-reranker" in state, "Non-cleared provider must remain in file"
