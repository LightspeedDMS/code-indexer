"""Tests for ProviderHealthMonitor file-backed persistence (Story #691).

Tests the optional persistence_path ctor parameter extension.
Uses real filesystem (tmp_path), real multiprocessing for concurrency tests.
No mocks of file I/O — anti-mock (Messi Rule 01).

Flock behavioral test design:
  The writer thread sets `about_to_sinbin` immediately before calling
  `m.sinbin()`. Construction only reads the file (no lock taken); so once
  `about_to_sinbin` is set, the very next file operation is the LOCK_EX
  acquire inside `_persist_to_file`. This gives the main process a
  deterministic signal for WHEN to release the child's lock -- no sleep is
  needed for THAT handshake. TestFlockUsed additionally proves the writer
  is still genuinely blocked right before release using TWO zero-sleep
  signals (Bug #1823 H4 fix, replacing a prior fixed-duration
  time.sleep()+is_set() timing guess that only proved "not finished yet"):
  (1) an fd-verified `fcntl.flock(LOCK_EX)` reachability check confirming
  the writer's OWN lock request targets the exact expected sidecar lock
  file, and (2) an independent non-blocking flock probe against that same
  file, which must fail with EACCES/EAGAIN while the child still holds
  it -- an OS-enforced advisory-lock guarantee, not a timing assumption.
  See that class's own docstring for the full mechanism.
"""

import errno
import fcntl
import json
import logging
import multiprocessing
import multiprocessing.synchronize
import os
import threading
import time
import unittest.mock
from pathlib import Path
from typing import Any, cast

import pytest

import code_indexer.utils.file_locking as file_locking_module
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_monitor(path: Path) -> ProviderHealthMonitor:
    """Return a new ProviderHealthMonitor instance with persistence_path set."""
    return ProviderHealthMonitor(persistence_path=path)


def _read_state_file(path: Path) -> dict[Any, Any]:
    # cast needed: json.loads() returns Any; structure is dynamic JSON state dict
    return cast(dict[Any, Any], json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Module-level helpers (must be picklable for multiprocessing)
# ---------------------------------------------------------------------------


def _lock_holder(
    path_str: str,
    ready_event: multiprocessing.synchronize.Event,
    release_event: multiprocessing.synchronize.Event,
) -> None:
    """Child process: acquire LOCK_EX on the SIDECAR lock file
    (``path.name + ".lock"``), signal ready, wait for release.

    Bug #1823 root-cause fix: ``ProviderHealthMonitor._persist_to_file()``
    (the method this test exists to verify) locks a stable sidecar file
    via ``path.parent / (path.name + ".lock")`` -- NEVER the state file
    (``path``) itself, specifically so the locked inode is never swapped
    out from under a held lock by ``os.replace()``'s atomic rename. The
    state file is only ever locked (LOCK_SH, non-contending) by
    ``_load_from_file()`` during construction. Locking ``path`` here (the
    original, pre-#1823 version of this helper) made construction --
    NOT the write path this test claims to verify -- the accidental
    synchronization point, costing a real ~10s wall-clock wait for the
    child's own full timeout before the lock was ever released (confirmed
    via instrumented reproduction, Bug #1823 investigation). Locking the
    sidecar instead makes ``m.sinbin()`` (which calls ``_persist_to_file``)
    genuinely block on this held lock, matching the test's documented
    intent, and removes the accidental construction-time stall entirely.
    """
    path = Path(path_str)
    path.write_text("{}", encoding="utf-8")
    lock_path = path.parent / (path.name + ".lock")
    fd = open(lock_path, "a")
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
        logger_name = "code_indexer.services.provider_health_monitor"
        caplog.set_level(logging.WARNING, logger=logger_name)
        with caplog.at_level(logging.WARNING, logger=logger_name):
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

    Bug #1823 root-cause fix: `_lock_holder` now locks the SIDECAR
    `.lock` file (matching `_persist_to_file`'s actual `lock_path`)
    instead of the state file itself, which only the non-contending
    `LOCK_SH` read path touches. The pre-fix version made monitor
    CONSTRUCTION -- not the write path this test claims to verify -- the
    accidental synchronization point, costing a real ~10s wall-clock wait
    for the child's own timeout on every run.

    Synchronization design:
      - The writer thread sets `about_to_sinbin` immediately before calling
        `m.sinbin()`. Construction only reads the already-present `{}` file
        (no LOCK_EX taken in __init__). So once `about_to_sinbin` fires, the
        next file I/O is the LOCK_EX acquisition inside `_persist_to_file()`.
      - Bug #1823 H4: two ZERO-SLEEP signals, together proving the writer
        thread is STILL genuinely blocked right before release, replacing
        a prior fixed-duration time.sleep()+is_set() timing guess that
        only proved "not finished yet" (confirmed vacuous via a
        monkeypatched no-op flock experiment during the original Bug
        #1823 investigation):
          1. `entering_flock` -- the real (never-faked) `fcntl.flock`
             call is instrumented to resolve its `fd` argument via
             `/proc/self/fd` and confirm it targets the EXACT expected
             sidecar lock file, proving reachability of the correct
             resource (not just "some LOCK_EX call happened somewhere" --
             a "wrong resource" break was confirmed to slip past a
             fd-agnostic version of this check).
          2. An independent non-blocking `fcntl.flock(LOCK_EX | LOCK_NB)`
             probe against that same file from the main thread, which
             MUST fail with EACCES/EAGAIN while the child still holds the
             lock -- an OS-enforced advisory-lock guarantee, not a timing
             assumption.
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

            # Bug #1823 (H4): entering_flock proves the writer thread
            # genuinely REACHED and invoked the real (never-faked)
            # fcntl.flock(LOCK_EX) call AGAINST THE EXACT EXPECTED
            # sidecar lock file -- not merely that some LOCK_EX call
            # happened somewhere. Resolving `fd` via
            # /proc/self/fd (Linux-only, matches this project's
            # environment) closes a discrimination gap found while
            # building this fix: a broken write path that takes a real
            # LOCK_EX on the WRONG file (never contending with the
            # child's held lock) would fire a fd-agnostic check but
            # complete near-instantly regardless -- fd verification
            # makes entering_flock itself the reachability proof for the
            # CORRECT resource, and a future regression that drops the
            # acquisition entirely, OR targets the wrong file, makes
            # entering_flock never fire, failing the `wait(timeout=5)`
            # below loudly instead of passing vacuously.
            lock_path = path.parent / (path.name + ".lock")
            expected_lock_realpath = os.path.realpath(str(lock_path))
            entering_flock = threading.Event()
            real_flock = file_locking_module.fcntl.flock

            def _instrumented_flock(fd: int, operation: int) -> None:
                if operation == fcntl.LOCK_EX:
                    try:
                        resolved = os.path.realpath(os.readlink(f"/proc/self/fd/{fd}"))
                    except OSError:
                        resolved = None
                    if resolved == expected_lock_realpath:
                        entering_flock.set()
                real_flock(fd, operation)

            with unittest.mock.patch.object(
                file_locking_module.fcntl,
                "flock",
                side_effect=_instrumented_flock,
            ):
                t = threading.Thread(target=do_sinbin, daemon=True)
                t.start()

                # Wait deterministically for writer to reach the sinbin call boundary.
                assert about_to_sinbin.wait(timeout=5), (
                    "Writer thread did not signal about_to_sinbin within 5s"
                )
                assert entering_flock.wait(timeout=5), (
                    "Writer thread never reached a real fcntl.flock(LOCK_EX) "
                    "call against the expected sidecar lock file within 5s "
                    "-- the flock acquisition is missing (or targets the "
                    "wrong resource) in the write path"
                )

                # Bug #1823 (H4): replaces the prior time.sleep()+is_set()
                # timing guess -- which only proved "not finished yet" and
                # could pass vacuously under gate load -- with a
                # ZERO-SLEEP, deterministic check: an independent
                # non-blocking flock probe against the SAME sidecar lock
                # file the write path locks. The child process still
                # holds LOCK_EX (release_event has not been set yet), so
                # this probe MUST fail with EACCES/EAGAIN -- an
                # OS-enforced advisory-lock guarantee, not a timing
                # assumption. Combined with entering_flock above (proving
                # the writer's IDENTICAL LOCK_EX request against the
                # IDENTICAL file has already been issued), the probe
                # failing logically guarantees the writer's own request
                # cannot have succeeded either, making
                # `not write_done.is_set()` a necessary consequence
                # rather than a scheduling guess.
                with open(lock_path, "a", encoding="utf-8") as probe_fh:
                    try:
                        fcntl.flock(probe_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN):
                            raise
                        lock_still_exclusively_held = True
                    else:
                        fcntl.flock(probe_fh.fileno(), fcntl.LOCK_UN)
                        lock_still_exclusively_held = False

                assert lock_still_exclusively_held, (
                    "Non-blocking probe acquired the sidecar lock before "
                    "the child was released -- the child process released "
                    "its LOCK_EX earlier than this test expects "
                    "(test-setup invariant violated, not evidence about "
                    "the write path)"
                )
                assert not write_done.is_set(), (
                    "Monitor write completed while the sidecar lock file "
                    "was still exclusively held (confirmed via an "
                    "independent non-blocking flock probe, AND the writer "
                    "had already reached its own flock(LOCK_EX) request) "
                    "-- _persist_to_file() is not genuinely blocking on "
                    "the sidecar lock file (flock regression)"
                )

                # Writer is genuinely still blocked on the held lock — release child LOCK_EX.
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
# Scenario: _persist_to_file must merge with (not discard) externally-written
# state for other providers -- root-cause regression test for the lost-update
# race reproduced by TestConcurrentWrites above. Deterministic, single-process,
# no multiprocessing timing dependency.
# ---------------------------------------------------------------------------

# Arbitrary future cooldown window used to build a fake externally-persisted
# sinbin entry in TestPersistPreservesUnrelatedProvider below.
_EXTERNAL_ENTRY_TTL_SECONDS = 300.0


class TestPersistPreservesUnrelatedProvider:
    """_persist_to_file must not discard another provider's persisted entry.

    Root cause: _build_merged_state accepted an `existing` (currently
    persisted) dict but never used it -- merged state was built purely from
    this instance's own in-memory _sinbin_until, so any provider persisted by
    a different process/instance (and never loaded into THIS instance's
    memory) was silently dropped on the next write. This is a lost-update
    race: whichever writer runs last wins, discarding the other's change.
    """

    def test_persist_preserves_provider_written_externally_by_another_process(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "state.json"
        # Construct the monitor BEFORE any file exists, so __init__'s
        # _load_from_file() loads nothing -- this instance's in-memory state
        # knows only about what it itself does from here on, exactly like a
        # freshly-started sibling OS process in the real concurrency scenario.
        m = _fresh_monitor(path)

        # Simulate a concurrent process persisting a DIFFERENT provider's
        # sinbin state directly to the file, exactly as another
        # ProviderHealthMonitor instance's _persist_to_file() would.
        external_entry = {
            "cohere-reranker": {
                "sinbin_until_wall_seconds": time.time() + _EXTERNAL_ENTRY_TTL_SECONDS,
                "last_failure_kind": "sinbin",
            }
        }
        path.write_text(json.dumps(external_entry), encoding="utf-8")

        # This instance now sinbins a provider it DOES track -- triggers
        # _persist_to_file() for THIS instance only.
        m.sinbin("voyage-reranker")

        data = _read_state_file(path)
        assert "cohere-reranker" in data, (
            "Externally-persisted provider must survive a write for a "
            "different provider by this instance (lost-update race); "
            f"got file contents: {data}"
        )
        assert "voyage-reranker" in data, (
            "This instance's own sinbin write must still be persisted; "
            f"got file contents: {data}"
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


# ---------------------------------------------------------------------------
# BLOCKER 1: get_instance() with persistence_path installs file-backed singleton
# ---------------------------------------------------------------------------


class TestGetInstanceWithPersistencePath:
    """get_instance(persistence_path=...) must create a file-backed singleton.

    Regression tests for BLOCKER 1: cli.py previously called the constructor
    directly (ProviderHealthMonitor(persistence_path=...)) which created an
    ORPHAN instance, not the class singleton. Reranker clients calling
    get_instance() subsequently got a DIFFERENT, in-memory-only singleton.
    Fix: get_instance() now accepts persistence_path and installs it as the
    singleton when the singleton has not yet been created.
    """

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        ProviderHealthMonitor.reset_instance()
        yield
        ProviderHealthMonitor.reset_instance()

    def test_get_instance_with_path_creates_file_backed_singleton(
        self, tmp_path: Path
    ) -> None:
        """First get_instance(persistence_path=path) call installs persistent singleton.

        Writes a sinbinned state file, calls get_instance with that path,
        then calls get_instance() again with no args (as reranker clients do).
        Both calls must return the SAME object and the second call must see
        the persisted sinbin state.
        """
        sinbin_path = tmp_path / "state.json"
        sinbin_path.write_text(
            json.dumps(
                {
                    "voyage-reranker": {
                        "sinbin_until_wall_seconds": time.time() + 3600,
                        "last_failure_kind": "sinbin",
                    }
                }
            ),
            encoding="utf-8",
        )

        # CLI-style call: first caller installs singleton with persistence
        instance_a = ProviderHealthMonitor.get_instance(persistence_path=sinbin_path)

        # Reranker-client-style call: no args, must return the same singleton
        instance_b = ProviderHealthMonitor.get_instance()

        assert instance_a is instance_b, (
            "get_instance() with no args must return the same singleton "
            "installed by get_instance(persistence_path=...)"
        )
        assert instance_b.is_sinbinned("voyage-reranker"), (
            "Singleton returned by get_instance() must have loaded the "
            "persisted sinbin state — voyage-reranker should be sinbinned"
        )

    def test_get_instance_different_path_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """get_instance(persistence_path=other_path) logs WARNING when singleton exists with a different path."""
        path_a = tmp_path / "state_a.json"
        path_b = tmp_path / "state_b.json"

        # Create singleton with path_a
        ProviderHealthMonitor.get_instance(persistence_path=path_a)

        # Second call with different path must log WARNING and return existing singleton
        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.services.provider_health_monitor",
        ):
            instance = ProviderHealthMonitor.get_instance(persistence_path=path_b)

        assert instance._persistence_path == path_a, (
            "Existing singleton must be returned unchanged when path differs"
        )
        warning_msgs = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("persistence_path" in m for m in warning_msgs), (
            f"Expected WARNING about persistence_path mismatch, got: {warning_msgs}"
        )

    def test_get_instance_returns_same_singleton_on_second_call_no_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Second no-arg get_instance() call returns existing singleton silently.

        After cli.py installs the file-backed singleton via
        get_instance(persistence_path=...), reranker clients call get_instance()
        with no arguments. This must return the same object without logging any
        warnings, preserving the persistence configuration silently.
        """
        path = tmp_path / "state.json"

        # First call installs the persistent singleton (cli.py pattern)
        instance_a = ProviderHealthMonitor.get_instance(persistence_path=path)

        # Second call with no args (reranker client pattern)
        with caplog.at_level(logging.WARNING):
            instance_b = ProviderHealthMonitor.get_instance()

        assert instance_a is instance_b, (
            "No-arg get_instance() must return the same singleton"
        )
        warning_msgs = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert not warning_msgs, (
            f"No-arg get_instance() after path-based install must not warn, got: {warning_msgs}"
        )

    def test_none_to_real_path_emits_debug_not_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Bug #1177: None->real_path transition must emit DEBUG, never WARNING.

        semantic_query_manager.py creates the singleton with path=None before
        cli.py can supply a real persistence path. This is a benign ordering
        issue - no sin-bin state is lost because a path=None singleton never
        loaded any. Only a WARNING should fire when two *different* non-None
        paths compete (see test_real_path_a_to_real_path_b_still_warns).
        """
        logger_name = "code_indexer.services.provider_health_monitor"
        real_path = tmp_path / "reranker_state.json"

        # First call: singleton created with path=None (semantic_query_manager pattern)
        instance_a = ProviderHealthMonitor.get_instance()
        assert instance_a._persistence_path is None

        # Second call: cli.py supplies a real persistence path
        with caplog.at_level(logging.DEBUG, logger=logger_name):
            instance_b = ProviderHealthMonitor.get_instance(persistence_path=real_path)

        # Must return the existing singleton unchanged
        assert instance_b is instance_a, (
            "Existing singleton must be returned unchanged on None->real_path transition"
        )

        # Must NOT emit a WARNING
        warning_msgs = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert not warning_msgs, (
            f"None->real_path must not produce WARNING, got: {warning_msgs}"
        )

        # Must emit exactly one DEBUG record mentioning the ignored path
        debug_msgs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.DEBUG
            and "ignoring requested path" in r.getMessage()
        ]
        assert len(debug_msgs) == 1, (
            f"Expected exactly one DEBUG 'ignoring requested path' record, got: {debug_msgs}"
        )

    def test_real_path_a_to_real_path_b_still_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Bug #1177 regression guard: two different non-None paths must still WARNING.

        The genuinely dangerous case - two distinct persistence files competing
        for the singleton - must still emit a WARNING so operators can diagnose
        configuration mistakes.
        """
        path_a = tmp_path / "state_a.json"
        path_b = tmp_path / "state_b.json"

        # First call: singleton with a real path
        ProviderHealthMonitor.get_instance(persistence_path=path_a)

        # Second call: different real path -> must still WARNING
        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.services.provider_health_monitor",
        ):
            instance = ProviderHealthMonitor.get_instance(persistence_path=path_b)

        assert instance._persistence_path == path_a, (
            "Existing singleton must be returned unchanged when two non-None paths differ"
        )
        warning_msgs = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("persistence_path" in m for m in warning_msgs), (
            f"Expected WARNING about persistence_path mismatch, got: {warning_msgs}"
        )

    def test_none_to_none_no_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """Bug #1177: None->None (both calls without path) produces no log output at all."""
        logger_name = "code_indexer.services.provider_health_monitor"

        # First call: no path
        ProviderHealthMonitor.get_instance()

        # Second call: also no path - must produce zero records for this logger
        with caplog.at_level(logging.DEBUG, logger=logger_name):
            caplog.clear()
            ProviderHealthMonitor.get_instance()

        all_msgs = [r.getMessage() for r in caplog.records]
        assert not all_msgs, (
            f"None->None second call must produce no log records at all, got: {all_msgs}"
        )
