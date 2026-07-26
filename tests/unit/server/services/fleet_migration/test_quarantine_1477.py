"""Tests for fleet-migration per-repo failure quarantine (Issue #1477).

`FleetMigrationScheduler._run_next_candidate()` always picks the FIRST
not-yet-migrated golden repo (alias-sorted order) with no memory of prior
attempts, so a repo whose migration throws every single time (e.g.
genuinely corrupt legacy `vector_*.json` data that
`scan_vectors_for_id_map` correctly refuses to auto-resolve) is retried
forever and permanently starves every alphabetically-later repo in the
fleet.

This module implements the quarantine mechanism: N consecutive failures
for a golden_alias quarantine it (skipped by the scheduler), persisted via
the SAME golden-repo-metadata storage backend GoldenRepoManager already
uses (`_sqlite_backend` -- SQLite solo / PostgreSQL cluster), mirroring
`golden_repo_reconciler.py`'s own breaker-state backend-injection
convention (Messi Rule #4: anti-duplication, no new storage mechanism).

Real SQLite backend + real on-disk directories throughout -- no mocking of
the quarantine module's own logic.
"""

import os
import tempfile
import time
from pathlib import Path

import pytest

from code_indexer.server.services.fleet_migration.discovery import (
    FleetMigrationCandidate,
)
from code_indexer.server.services.fleet_migration.quarantine import (
    DISK_HEADROOM_FAILURE_CAUSE,
    FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
    GENERIC_FAILURE_CAUSE,
    QuarantineStateUnavailableError,
    classify_failure_cause,
    compute_repo_state_signature,
    count_quarantined,
    get_failure_state,
    is_quarantined,
    probe_quarantine_backend_health,
    record_migration_failure,
    reset_migration_failure,
    status_counts_as_quarantine_failure,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend
from code_indexer.global_repos.alias_manager import AliasManager


class _FakeGoldenRepoManagerWithBackend:
    """Minimal test double carrying a REAL `_sqlite_backend` attribute --
    the exact attribute name `golden_repo_reconciler.py`'s own
    `_get_breaker_backend()` helper reuses from `GoldenRepoManager`."""

    def __init__(self, sqlite_backend):
        self._sqlite_backend = sqlite_backend


class _FakeGoldenRepoManagerNoBackend:
    """No `_sqlite_backend` attribute at all -- callers must degrade
    gracefully (never raise) rather than assuming it always exists."""


class _AlwaysFailingBackend:
    """Simulates a PERSISTENT backend read failure (Finding A, Codex
    round-3 review) -- e.g. a real DB/connection outage -- for
    `get_fleet_migration_failure_state()`. Distinct from
    `_FakeGoldenRepoManagerNoBackend` (no backend configured at all,
    tracking deliberately disabled): here a backend EXISTS but its query
    genuinely fails every time."""

    def get_fleet_migration_failure_state(self, golden_alias: str):
        raise RuntimeError("simulated persistent backend outage")


class _AlwaysFailingWriteBackend:
    """Simulates a PERSISTENT backend WRITE failure (Finding D, Codex
    round-4 review, live-reproduced) -- the read path is irrelevant here
    (never exercised by `record_migration_failure` itself), but
    `record_fleet_migration_failure()` genuinely fails every time."""

    def record_fleet_migration_failure(
        self, golden_alias: str, state_signature: str, failure_cause=None
    ):
        raise RuntimeError("simulated persistent backend write outage")


class _AlwaysFailingResetBackend:
    """Reads/writes succeed normally (backed by a real in-memory dict),
    but `reset_fleet_migration_failure()` always fails (Finding H, Codex
    round-5 review, live-reproduced) -- simulates UPDATE working while
    DELETE is specifically broken."""

    def __init__(self):
        self._states: dict = {}

    def get_fleet_migration_failure_state(self, golden_alias: str):
        return self._states.get(golden_alias)

    def record_fleet_migration_failure(
        self, golden_alias: str, state_signature: str, failure_cause=None
    ):
        row = self._states.setdefault(
            golden_alias,
            {
                "golden_alias": golden_alias,
                "consecutive_failure_count": 0,
                "state_signature": None,
                "signature_checked_at": None,
                "failure_cause": None,
            },
        )
        row["consecutive_failure_count"] += 1
        row["state_signature"] = state_signature
        row["failure_cause"] = failure_cause
        return row["consecutive_failure_count"]

    def reset_fleet_migration_failure(self, golden_alias: str) -> None:
        raise RuntimeError("simulated persistent backend DELETE outage")

    def touch_fleet_migration_failure_check(self, golden_alias: str) -> None:
        row = self._states.get(golden_alias)
        if row is not None:
            row["signature_checked_at"] = "touched"

    def list_fleet_migration_failure_states(self):
        return list(self._states.values())


class _ResetFailsSoftResetSucceedsBackend(_AlwaysFailingResetBackend):
    """Finding N (MEDIUM, Codex round-7 review): the full reset (DELETE)
    is broken, but the new soft-reset (UPDATE consecutive_failure_count
    = 0, keeping the row) works -- the same DELETE-vs-UPDATE asymmetry
    Finding H/K already established, now exercised for the fallback
    path."""

    def soft_reset_fleet_migration_failure_count(self, golden_alias: str) -> None:
        row = self._states.get(golden_alias)
        if row is not None:
            row["consecutive_failure_count"] = 0


class _BothResetAndSoftResetFailBackend(_AlwaysFailingResetBackend):
    """Finding N deepest fallback: BOTH the full reset (DELETE) and the
    soft-reset (UPDATE) are broken -- proceeding without resetting
    anything must still never raise/crash. This is the scenario the
    SEPARATE write-health probe (Finding G/J) is relied upon to
    independently catch on the next tick."""

    def soft_reset_fleet_migration_failure_count(self, golden_alias: str) -> None:
        raise RuntimeError("simulated persistent backend soft-reset UPDATE outage")


@pytest.fixture
def sqlite_backend():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        be = GoldenRepoMetadataSqliteBackend(db_path)
        be.ensure_table_exists()
        yield be


def _make_candidate(tmp_path: Path, alias: str = "click") -> FleetMigrationCandidate:
    base_clone = tmp_path / alias
    index_path = base_clone / ".code-indexer" / "index"
    collection_dir = index_path / "semantic_collection"
    collection_dir.mkdir(parents=True)
    (collection_dir / "collection_meta.json").write_text("{}")

    return FleetMigrationCandidate(
        sort_key=alias,
        golden_alias=alias,
        base_clone_path=base_clone,
        index_path=index_path,
        semantic_collection_dirs=[collection_dir],
        temporal_namespaces=[],
        sister_root=tmp_path / "golden-repos",
        sister_alias_manager=AliasManager(str(tmp_path / "golden-repos" / "aliases")),
    )


class TestComputeRepoStateSignatureStability:
    def test_signature_is_stable_across_repeated_calls_with_no_change(
        self, tmp_path: Path
    ) -> None:
        candidate = _make_candidate(tmp_path)
        sig1 = compute_repo_state_signature(candidate)
        sig2 = compute_repo_state_signature(candidate)
        assert sig1 == sig2

    def test_signature_changes_when_a_file_is_added_to_a_collection_dir(
        self, tmp_path: Path
    ) -> None:
        candidate = _make_candidate(tmp_path)
        sig_before = compute_repo_state_signature(candidate)

        (candidate.semantic_collection_dirs[0] / "new_shard_entry").mkdir()

        sig_after = compute_repo_state_signature(candidate)
        assert sig_before != sig_after

    def test_signature_changes_when_the_collection_dir_is_deleted(
        self, tmp_path: Path
    ) -> None:
        import shutil

        candidate = _make_candidate(tmp_path)
        sig_before = compute_repo_state_signature(candidate)

        shutil.rmtree(candidate.semantic_collection_dirs[0])

        sig_after = compute_repo_state_signature(candidate)
        assert sig_before != sig_after


class TestRecordAndResetMigrationFailure:
    def test_record_migration_failure_persists_via_backend(
        self, sqlite_backend
    ) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)

        record_migration_failure(golden_repo_manager, "click", "sig-1")

        state = get_failure_state(golden_repo_manager, "click")
        assert state is not None
        assert state["consecutive_failure_count"] == 1
        assert state["state_signature"] == "sig-1"

    def test_reset_migration_failure_clears_state(self, sqlite_backend) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)
        record_migration_failure(golden_repo_manager, "click", "sig-1")

        reset_migration_failure(golden_repo_manager, "click")

        assert get_failure_state(golden_repo_manager, "click") is None

    def test_degrades_gracefully_with_no_backend_attribute(self) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerNoBackend()

        # Must never raise -- a missing backend must never block migration.
        record_migration_failure(golden_repo_manager, "click", "sig-1")
        reset_migration_failure(golden_repo_manager, "click")
        assert get_failure_state(golden_repo_manager, "click") is None


class TestIsQuarantined:
    def test_not_quarantined_when_never_failed(
        self, tmp_path: Path, sqlite_backend
    ) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)
        candidate = _make_candidate(tmp_path)

        assert is_quarantined(golden_repo_manager, candidate) is False

    def test_not_quarantined_below_threshold(
        self, tmp_path: Path, sqlite_backend
    ) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)
        candidate = _make_candidate(tmp_path)
        signature = compute_repo_state_signature(candidate)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD - 1):
            record_migration_failure(
                golden_repo_manager, candidate.golden_alias, signature
            )

        assert is_quarantined(golden_repo_manager, candidate) is False

    def test_quarantined_once_threshold_reached_with_unchanged_state(
        self, tmp_path: Path, sqlite_backend
    ) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)
        candidate = _make_candidate(tmp_path)
        signature = compute_repo_state_signature(candidate)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(
                golden_repo_manager, candidate.golden_alias, signature
            )

        assert is_quarantined(golden_repo_manager, candidate) is True

    def test_quarantine_auto_clears_when_on_disk_state_genuinely_changes(
        self, tmp_path: Path, sqlite_backend
    ) -> None:
        """Mirrors description_refresh_scheduler.py's commit-based
        auto-clear gate (Bug #1096): quarantine holds while the on-disk
        state is unchanged; clears ONLY when it genuinely changes -- never
        on a bare retry."""
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)
        candidate = _make_candidate(tmp_path)
        signature = compute_repo_state_signature(candidate)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(
                golden_repo_manager, candidate.golden_alias, signature
            )
        assert is_quarantined(golden_repo_manager, candidate) is True

        # Genuine on-disk state change (e.g. operator remediation).
        (candidate.semantic_collection_dirs[0] / "repaired_marker").mkdir()

        # recheck_interval_seconds=0 bypasses Finding C's throttle window
        # (a separate, dedicated concern covered by
        # TestIsQuarantinedThrottlesExpensiveRecheck below) -- THIS test
        # validates the auto-clear-on-genuine-change DETECTION logic
        # itself, independent of real elapsed wall-clock time.
        assert (
            is_quarantined(golden_repo_manager, candidate, recheck_interval_seconds=0)
            is False
        )
        # The auto-clear must reset the persisted state as a side effect,
        # so the caller does not need a second cleanup step.
        assert get_failure_state(golden_repo_manager, candidate.golden_alias) is None

    def test_degrades_gracefully_with_no_backend_attribute(
        self, tmp_path: Path
    ) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerNoBackend()
        candidate = _make_candidate(tmp_path)

        assert is_quarantined(golden_repo_manager, candidate) is False


class TestCountQuarantined:
    def test_counts_only_aliases_at_or_above_threshold(
        self, tmp_path: Path, sqlite_backend
    ) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)
        click_candidate = _make_candidate(tmp_path, "click")
        evolution_candidate = _make_candidate(tmp_path, "evolution")
        click_sig = compute_repo_state_signature(click_candidate)
        evolution_sig = compute_repo_state_signature(evolution_candidate)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(golden_repo_manager, "click", click_sig)
        record_migration_failure(golden_repo_manager, "evolution", evolution_sig)

        count = count_quarantined(golden_repo_manager, ["click", "evolution"])
        assert count == 1

    def test_zero_when_no_failures_recorded(
        self, tmp_path: Path, sqlite_backend
    ) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)
        assert count_quarantined(golden_repo_manager, ["click", "evolution"]) == 0

    def test_degrades_gracefully_with_no_backend_attribute(self) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerNoBackend()
        assert count_quarantined(golden_repo_manager, ["click"]) == 0


class TestComputeRepoStateSignatureNestedShardSensitivity:
    """Finding 1 (dual code-review round): the real corrupt data for Issue
    #1477's scenario lives in NESTED hash-shard subdirectories
    (collection_dir/aa/bb/vector_*.json), not directly inside the
    collection root. A signature that only fingerprints the top-level
    directory cannot detect an operator deleting/replacing a file deep
    inside a shard subdirectory -- the exact real remediation for this
    corruption -- leaving a quarantine stuck forever with no way to
    auto-clear."""

    def test_signature_changes_when_a_file_deep_in_a_nested_shard_dir_is_removed(
        self, tmp_path: Path
    ) -> None:
        candidate = _make_candidate(tmp_path)
        collection_dir = candidate.semantic_collection_dirs[0]
        shard_dir_a = collection_dir / "aa" / "bb"
        shard_dir_a.mkdir(parents=True)
        (shard_dir_a / "vector_dupe_a.json").write_text("{}")
        shard_dir_b = collection_dir / "cc" / "dd"
        shard_dir_b.mkdir(parents=True)
        duplicate_file = shard_dir_b / "vector_dupe_b.json"
        duplicate_file.write_text("{}")

        sig_before = compute_repo_state_signature(candidate)

        # Delete ONE nested duplicate -- the real operator remediation --
        # WITHOUT touching the top-level collection directory itself.
        duplicate_file.unlink()

        sig_after = compute_repo_state_signature(candidate)
        assert sig_before != sig_after

    def test_signature_changes_when_a_new_file_is_added_deep_in_a_nested_shard_dir(
        self, tmp_path: Path
    ) -> None:
        candidate = _make_candidate(tmp_path)
        collection_dir = candidate.semantic_collection_dirs[0]
        shard_dir = collection_dir / "aa" / "bb"
        shard_dir.mkdir(parents=True)
        (shard_dir / "vector_existing.json").write_text("{}")

        sig_before = compute_repo_state_signature(candidate)

        (shard_dir / "vector_new.json").write_text("{}")

        sig_after = compute_repo_state_signature(candidate)
        assert sig_before != sig_after


class TestComputeRepoStateSignatureDetectsInPlaceContentRewrites:
    """Finding B (Codex round-3 review): rewriting an EXISTING file's
    content in place (same filename, same parent directory, no add/
    remove of any directory entry) MUST change the signature -- directory
    mtime/entry-count alone (the round-2 implementation) cannot see this,
    since POSIX directory mtime only changes on add/remove/rename, never
    on an existing file's content being rewritten. Without this, a repo
    repaired by rewriting a corrupt file's content in place (rather than
    deleting/replacing it) could stay quarantined forever. The fix
    incorporates each leaf file's own mtime_ns/size into the fingerprint,
    not just the containing directory's own metadata."""

    def test_rewriting_an_existing_files_content_changes_the_signature(
        self, tmp_path: Path
    ) -> None:
        candidate = _make_candidate(tmp_path)
        collection_dir = candidate.semantic_collection_dirs[0]
        shard_dir = collection_dir / "aa" / "bb"
        shard_dir.mkdir(parents=True)
        target_file = shard_dir / "vector_existing.json"
        target_file.write_text('{"id": "original"}')

        sig_before = compute_repo_state_signature(candidate)

        # Rewrite the SAME file's content in place -- no add/remove of any
        # directory entry, only the file's own content/mtime/size change.
        target_file.write_text('{"id": "rewritten-with-different-content"}')

        sig_after = compute_repo_state_signature(candidate)
        assert sig_before != sig_after


class TestComputeRepoStateSignatureDetectsSizeAndMtimePreservedRewrite:
    """Finding E (MEDIUM, residual, Codex round-4 review): Finding B's
    fix -- folding `(name, mtime_ns, size)` into the leaf-file token --
    correctly detects an ORDINARY content rewrite (which normally changes
    mtime), but a rewrite that preserves BOTH the original size AND the
    original mtime (e.g. a tool that explicitly restores timestamps
    after writing, or a coarse-timestamp filesystem) is still invisible.
    `st_ctime_ns` (inode change time) closes this -- it changes on
    essentially any real content or metadata modification, including
    ones that deliberately preserve mtime, and is free: the SAME
    `stat()`/`DirEntry.stat()` call already made, no new syscalls."""

    def test_content_rewrite_preserving_exact_size_and_mtime_still_changes_signature(
        self, tmp_path: Path
    ) -> None:
        candidate = _make_candidate(tmp_path)
        collection_dir = candidate.semantic_collection_dirs[0]
        shard_dir = collection_dir / "aa" / "bb"
        shard_dir.mkdir(parents=True)
        target_file = shard_dir / "vector_existing.json"
        original_content = '{"id": "original"}'
        target_file.write_text(original_content)
        original_stat = target_file.stat()

        sig_before = compute_repo_state_signature(candidate)

        # A real, short wall-clock gap: empirically confirmed on this
        # filesystem that ctime does not advance within the same tick,
        # so without a genuine gap between the original write and the
        # rewrite, ctime cannot distinguish "before" from "after" even
        # when the fix is correct.
        time.sleep(0.01)

        # Rewrite with content of the EXACT SAME byte length, then
        # explicitly restore the original mtime/atime via os.utime() --
        # simulating a tool that preserves timestamps after writing, or
        # a coarse-timestamp filesystem where two writes land in the
        # same tick.
        rewritten_content = '{"id": "replaced"}'
        assert len(rewritten_content) == len(original_content)
        target_file.write_text(rewritten_content)
        os.utime(
            target_file,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        assert target_file.stat().st_size == original_stat.st_size
        assert target_file.stat().st_mtime_ns == original_stat.st_mtime_ns

        sig_after = compute_repo_state_signature(candidate)
        assert sig_before != sig_after


class TestStatusCountsAsQuarantineFailure:
    """Finding 2 (Codex-found, reproduced concretely): every possible
    orchestrator result status must be EXPLICITLY classified as counting
    toward the quarantine breaker or not -- never an implicit fallthrough
    that could silently reproduce Issue #1477's starvation bug via a
    non-exception status like "incomplete"."""

    def test_completed_never_counts(self) -> None:
        assert status_counts_as_quarantine_failure("completed") is False

    @pytest.mark.parametrize("transient_status", ["lock_held", "refresh_in_flight"])
    def test_transient_statuses_do_not_count(self, transient_status: str) -> None:
        assert status_counts_as_quarantine_failure(transient_status) is False

    @pytest.mark.parametrize(
        "no_progress_status", ["incomplete", "refused_immutable_path"]
    )
    def test_no_progress_statuses_count(self, no_progress_status: str) -> None:
        assert status_counts_as_quarantine_failure(no_progress_status) is True

    def test_unrecognized_future_status_counts_by_default(self) -> None:
        """Fail-conservative: an unclassified future status must count
        toward quarantine by default -- the safe direction, since NOT
        counting a genuine no-progress status is exactly what reproduces
        Issue #1477's fleet-starvation bug."""
        assert status_counts_as_quarantine_failure("some_brand_new_status") is True


class TestGetFailureStateRaisesOnBackendReadFailure:
    """Finding A (HIGH, Codex round-3 review, live-reproduced): a
    PERSISTENT backend read failure must NEVER be silently treated as
    "not quarantined" -- that recreates the EXACT fleet-starvation bug
    #1477 reports (the scheduler would retry the SAME broken candidate
    forever), just triggered by a backend outage instead of corrupt data.
    `get_failure_state()`/`is_quarantined()` must raise a typed
    `QuarantineStateUnavailableError` instead of swallowing the failure
    into a return value -- the caller (the scheduler) is then responsible
    for aborting the scheduling tick rather than guessing."""

    def test_get_failure_state_raises_on_backend_query_failure(self) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(_AlwaysFailingBackend())

        with pytest.raises(QuarantineStateUnavailableError):
            get_failure_state(golden_repo_manager, "click")

    def test_is_quarantined_propagates_the_same_exception(self, tmp_path: Path) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(_AlwaysFailingBackend())
        candidate = _make_candidate(tmp_path)

        with pytest.raises(QuarantineStateUnavailableError):
            is_quarantined(golden_repo_manager, candidate)


#: Named constants for TestIsQuarantinedThrottlesExpensiveRecheck (Finding
#: C) -- avoids magic numbers in the test bodies below.
_THROTTLE_TEST_REPEATED_CALL_COUNT = 5
_THROTTLE_TEST_SHORT_INTERVAL_SECONDS = 0.05
_THROTTLE_TEST_SLEEP_MULTIPLIER = 2


class TestIsQuarantinedThrottlesExpensiveRecheck:
    """Finding C (MEDIUM, high relevance to this epic's own AC4 large-repo
    test, Codex round-3 review): Codex measured the full nested-shard
    signature walk at ~0.5s / ~45,338 stat() calls / ~27,020 iterdir()
    calls / ~22,668 is_dir() calls on a synthetic 21,518-file collection
    deliberately matching the real "evolution" golden repo's actual scale.
    `is_quarantined()` calls this walk for EVERY already-quarantined
    candidate the scheduler's per-candidate loop encounters, on EVERY
    tick -- an I/O storm on NFS (this project's actual staging/cluster
    storage) for as long as a large repo stays quarantined.

    A throttled recheck cadence must gate WHEN the expensive walk runs;
    it must NEVER by itself clear quarantine (only an ACTUAL detected
    on-disk change may do that).

    `_install_signature_call_spy` is a call-COUNTING SPY, not a mock of
    the system under test's business logic: it WRAPS the real
    `compute_repo_state_signature` (every call still executes the actual
    walk against the real on-disk fixture and returns the genuine result)
    -- it exists SOLELY to observe how many times `is_quarantined()`
    invokes this expensive collaborator, which is precisely the
    interaction the round-3 dual review mandated proving via an actual
    call count (not a proxy metric). The real walk's own correctness is
    exhaustively covered elsewhere (TestComputeRepoStateSignature* classes
    above) with zero mocking.
    """

    def _install_signature_call_spy(self, monkeypatch):
        import code_indexer.server.services.fleet_migration.quarantine as quarantine_module

        call_count = {"n": 0}
        real_compute = quarantine_module.compute_repo_state_signature

        def _counting_compute(candidate):
            call_count["n"] += 1
            return real_compute(candidate)

        monkeypatch.setattr(
            quarantine_module, "compute_repo_state_signature", _counting_compute
        )
        return call_count

    def test_within_the_throttle_window_the_expensive_walk_is_not_repeated(
        self, tmp_path: Path, sqlite_backend, monkeypatch
    ) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)
        candidate = _make_candidate(tmp_path)
        signature = compute_repo_state_signature(candidate)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(
                golden_repo_manager, candidate.golden_alias, signature
            )

        call_count = self._install_signature_call_spy(monkeypatch)

        # Several calls in quick succession, well within the (large,
        # production-default) throttle window record_migration_failure()
        # just established -- the expensive walk must NOT run at all.
        for _ in range(_THROTTLE_TEST_REPEATED_CALL_COUNT):
            assert is_quarantined(golden_repo_manager, candidate) is True

        assert call_count["n"] == 0

    def test_after_the_throttle_window_elapses_a_genuine_change_is_detected(
        self, tmp_path: Path, sqlite_backend, monkeypatch
    ) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)
        candidate = _make_candidate(tmp_path)
        signature = compute_repo_state_signature(candidate)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(
                golden_repo_manager, candidate.golden_alias, signature
            )

        call_count = self._install_signature_call_spy(monkeypatch)

        time.sleep(
            _THROTTLE_TEST_SHORT_INTERVAL_SECONDS * _THROTTLE_TEST_SLEEP_MULTIPLIER
        )

        # Genuine on-disk state change (e.g. operator remediation).
        (candidate.semantic_collection_dirs[0] / "repaired_marker").mkdir()

        result = is_quarantined(
            golden_repo_manager,
            candidate,
            recheck_interval_seconds=_THROTTLE_TEST_SHORT_INTERVAL_SECONDS,
        )

        assert result is False
        assert call_count["n"] == 1
        assert get_failure_state(golden_repo_manager, candidate.golden_alias) is None


class TestRecordMigrationFailureRaisesOnBackendWriteFailure:
    """Finding D (HIGH, Codex round-4 review, live-reproduced): a
    PERSISTENT backend WRITE failure must NEVER be silently swallowed --
    with `record_migration_failure()` swallowing every write exception,
    the consecutive-failure count is NEVER incremented, so
    `is_quarantined()` keeps reading "0 failures, not quarantined"
    forever and the SAME corrupt repo is retried on every tick,
    recreating Issue #1477's exact fleet-starvation bug via a write-path
    outage instead of corrupt data or a read-path outage. This mirrors
    Finding A's read-side fix exactly: raise instead of swallow."""

    def test_record_migration_failure_raises_on_backend_write_failure(self) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(
            _AlwaysFailingWriteBackend()
        )

        with pytest.raises(QuarantineStateUnavailableError):
            record_migration_failure(golden_repo_manager, "click", "sig-1")


class TestResetMigrationFailureRaisesOnBackendDeleteFailure:
    """Finding H (MEDIUM, Codex round-5 review, live-reproduced): a
    PERSISTENT backend DELETE failure must NEVER be silently swallowed --
    the round-4 `reset_migration_failure()` logged a reset failure but
    still behaved as if it succeeded, which can report "cleared" when
    the row was never actually deleted (UPDATE still works, DELETE is
    specifically broken)."""

    def test_reset_migration_failure_raises_on_backend_delete_failure(self) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(
            _AlwaysFailingResetBackend()
        )

        with pytest.raises(QuarantineStateUnavailableError):
            reset_migration_failure(golden_repo_manager, "click")


class TestIsQuarantinedClearsDespiteResetFailureWhenRepairDetected:
    """Finding K (HIGH, Codex round-6 review, live-reproduced): a
    reset/DELETE-only outage must NEVER block the entire fleet -- this
    directly contradicted Finding H's own principle ("a broken reset
    must never block migration"). `is_quarantined()` must distinguish
    "we couldn't determine the CURRENT read-state at all" (Finding A --
    must still abort, see TestGetFailureStateRaisesOnBackendReadFailure)
    from "we detected GENUINE repair evidence (signature changed / disk
    headroom now sufficient) but the cleanup/reset call failed" (must
    NOT abort -- the READ itself succeeded and told us this repo is no
    longer failing, so we return False/"not quarantined, safe to retry"
    and let migration proceed, even though the stale row lingers). The
    stale row is self-healing: either the next successful reset clears
    it, or the next recorded failure atomically overwrites it."""

    def test_signature_based_auto_clear_returns_false_despite_reset_failure(
        self, tmp_path: Path
    ) -> None:
        backend = _AlwaysFailingResetBackend()
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(backend)
        candidate = _make_candidate(tmp_path)
        signature = compute_repo_state_signature(candidate)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(
                golden_repo_manager, candidate.golden_alias, signature
            )

        # Genuine on-disk state change (e.g. operator remediation) --
        # is_quarantined() detects this via the signature and attempts
        # to auto-clear, which requires a reset() call that is broken in
        # this backend. The READ succeeded and confirmed the repair --
        # this must NOT abort.
        (candidate.semantic_collection_dirs[0] / "repaired_marker").mkdir()

        result = is_quarantined(
            golden_repo_manager, candidate, recheck_interval_seconds=0
        )

        assert result is False
        # The stale row lingers (reset failed) -- this is expected and
        # self-healing, never a data-integrity risk.
        assert (
            get_failure_state(golden_repo_manager, candidate.golden_alias) is not None
        )

    def test_disk_headroom_based_auto_clear_returns_false_despite_reset_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import code_indexer.server.services.fleet_migration.quarantine as quarantine_module

        backend = _AlwaysFailingResetBackend()
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(backend)
        candidate = _make_candidate(tmp_path)
        signature = compute_repo_state_signature(candidate)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(
                golden_repo_manager,
                candidate.golden_alias,
                signature,
                failure_cause=DISK_HEADROOM_FAILURE_CAUSE,
            )

        # The disk-headroom oracle now reports sufficient space -- the
        # READ-derived decision confirms the repair -- but reset is
        # broken in this backend. Must NOT abort.
        monkeypatch.setattr(
            quarantine_module, "_disk_headroom_currently_sufficient", lambda c: True
        )

        result = is_quarantined(golden_repo_manager, candidate)

        assert result is False
        assert (
            get_failure_state(golden_repo_manager, candidate.golden_alias) is not None
        )


class TestIsQuarantinedSoftResetsFailureCountWhenFullResetFails:
    """Finding N (MEDIUM, Codex round-7 review): Finding K's fix let
    migration proceed when a repair is detected but the full reset
    (DELETE) fails, reasoning the stale row is "self-healing" because
    the NEXT recorded failure would overwrite it. Codex proved that
    reasoning only partially true: `record_migration_failure` never
    resets `consecutive_failure_count` back to 1 -- it just increments
    the STALE count (e.g. 3 -> 4). A just-repaired repo that legitimately
    fails once more for an unrelated reason immediately re-quarantines,
    never getting its intended fresh 3-attempt budget. The fix: attempt
    a soft-reset (UPDATE consecutive_failure_count = 0, keep the row) as
    a fallback before falling back further to "log and proceed without
    resetting anything"."""

    def test_soft_reset_gives_a_genuinely_fresh_failure_budget_when_full_reset_fails(
        self, tmp_path: Path
    ) -> None:
        backend = _ResetFailsSoftResetSucceedsBackend()
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(backend)
        candidate = _make_candidate(tmp_path)
        signature = compute_repo_state_signature(candidate)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(
                golden_repo_manager, candidate.golden_alias, signature
            )

        # Genuine on-disk state change -- is_quarantined() detects this,
        # the full reset (DELETE) fails, but the soft-reset (UPDATE)
        # fallback must succeed and genuinely zero the stale count.
        (candidate.semantic_collection_dirs[0] / "repaired_marker").mkdir()

        result = is_quarantined(
            golden_repo_manager, candidate, recheck_interval_seconds=0
        )
        assert result is False

        state_after_repair = get_failure_state(
            golden_repo_manager, candidate.golden_alias
        )
        assert state_after_repair is not None
        assert state_after_repair["consecutive_failure_count"] == 0

        # One new, unrelated failure must start counting from a genuinely
        # fresh budget (1, not 4) -- and must NOT immediately re-quarantine.
        new_signature = compute_repo_state_signature(candidate)
        record_migration_failure(
            golden_repo_manager, candidate.golden_alias, new_signature
        )
        state_after_one_new_failure = get_failure_state(
            golden_repo_manager, candidate.golden_alias
        )
        assert state_after_one_new_failure is not None
        assert state_after_one_new_failure["consecutive_failure_count"] == 1

        assert (
            is_quarantined(golden_repo_manager, candidate, recheck_interval_seconds=0)
            is False
        )

    def test_proceeds_without_crashing_when_both_full_reset_and_soft_reset_fail(
        self, tmp_path: Path
    ) -> None:
        backend = _BothResetAndSoftResetFailBackend()
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(backend)
        candidate = _make_candidate(tmp_path)
        signature = compute_repo_state_signature(candidate)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(
                golden_repo_manager, candidate.golden_alias, signature
            )

        (candidate.semantic_collection_dirs[0] / "repaired_marker").mkdir()

        # Neither the full reset nor the soft-reset fallback can write --
        # this must still never raise, and migration must still be
        # allowed to proceed (the READ already confirmed the repair).
        result = is_quarantined(
            golden_repo_manager, candidate, recheck_interval_seconds=0
        )
        assert result is False

        # The stale, elevated count lingers -- both write paths are
        # broken, so nothing could be corrected. This is the bounded,
        # deepest-fallback residual the write-health probe (Finding G/J)
        # independently catches on the next tick.
        stale_state = get_failure_state(golden_repo_manager, candidate.golden_alias)
        assert stale_state is not None
        assert (
            stale_state["consecutive_failure_count"]
            == FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD
        )


class TestProbeQuarantineBackendHealth:
    """Finding G (HIGH, Codex round-5 review, live-reproduced): a cheap,
    non-destructive write+read round-trip against the SAME quarantine
    backend, used to detect whether a previously-observed write-
    bookkeeping outage has recovered -- BEFORE the scheduler re-attempts
    the expensive/destructive migration call."""

    def test_probe_succeeds_against_a_real_healthy_backend(
        self, sqlite_backend
    ) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)

        assert probe_quarantine_backend_health(golden_repo_manager) is True

    def test_probe_does_not_leave_stray_state_on_a_real_backend(
        self, sqlite_backend
    ) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)

        probe_quarantine_backend_health(golden_repo_manager)

        assert sqlite_backend.list_fleet_migration_failure_states() == []

    def test_probe_fails_against_a_backend_whose_writes_are_broken(self) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(
            _AlwaysFailingWriteBackend()
        )

        assert probe_quarantine_backend_health(golden_repo_manager) is False


class TestProbeQuarantineBackendHealthWithBrokenResetOnly:
    """The probe must never conflate Finding G's real concern ("can we
    WRITE a new failure record") with a broken RESET/cleanup step
    (Finding H's concern -- a reset/DELETE failure must never block
    migration attempts). A backend whose WRITE succeeds but whose RESET
    specifically fails (mirroring `_AlwaysFailingResetBackend`, e.g. a
    real UPDATE-works-DELETE-broken scenario) must still report the
    probe as healthy -- the sentinel row's cleanup is best-effort, not
    health-determining."""

    def test_probe_succeeds_even_when_only_reset_is_broken(self) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(
            _AlwaysFailingResetBackend()
        )

        assert probe_quarantine_backend_health(golden_repo_manager) is True


class TestProbeQuarantineBackendHealthNoBackendConfigured:
    def test_probe_succeeds_when_no_backend_is_configured_at_all(self) -> None:
        golden_repo_manager = _FakeGoldenRepoManagerNoBackend()

        # Nothing to probe -- deliberately "tracking disabled" is treated
        # as healthy, not as an outage.
        assert probe_quarantine_backend_health(golden_repo_manager) is True


class TestClassifyFailureCause:
    """Finding I (MEDIUM, Codex round-5 review): distinguishes a
    disk-headroom-caused failure (clears via re-evaluating the SAME
    disk-space oracle orchestrator.py's preflight uses, independent of
    directory content) from a corrupt-data/exception-caused one (clears
    via the directory-content signature, unchanged)."""

    def test_no_detail_classifies_as_generic(self) -> None:
        assert classify_failure_cause(detail=None) == GENERIC_FAILURE_CAUSE

    def test_disk_headroom_detail_classifies_as_disk_headroom(self) -> None:
        assert (
            classify_failure_cause(
                detail=(
                    "one or more semantic collections were skipped for "
                    "insufficient disk headroom"
                )
            )
            == DISK_HEADROOM_FAILURE_CAUSE
        )

    def test_unrelated_detail_classifies_as_generic(self) -> None:
        assert (
            classify_failure_cause(
                detail="residual in-repo temporal directories remain"
            )
            == GENERIC_FAILURE_CAUSE
        )


class TestIsQuarantinedClearsDiskHeadroomCauseIndependently:
    """Finding I: a genuine disk-headroom repair (freeing up disk space
    ELSEWHERE on the same filesystem) can never be observed by the
    directory-content signature -- the auto-clear path for a
    disk-headroom-caused quarantine must instead re-evaluate the SAME
    disk-space oracle orchestrator.py's preflight uses, independent of
    any directory-content change."""

    def test_clears_when_the_disk_headroom_oracle_now_reports_sufficient_space(
        self, tmp_path: Path, sqlite_backend, monkeypatch
    ) -> None:
        import code_indexer.server.services.fleet_migration.quarantine as quarantine_module

        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)
        candidate = _make_candidate(tmp_path)
        signature = compute_repo_state_signature(candidate)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(
                golden_repo_manager,
                candidate.golden_alias,
                signature,
                failure_cause=DISK_HEADROOM_FAILURE_CAUSE,
            )

        # Still quarantined while the oracle reports insufficient space --
        # NO directory-content change made.
        monkeypatch.setattr(
            quarantine_module, "_disk_headroom_currently_sufficient", lambda c: False
        )
        assert is_quarantined(golden_repo_manager, candidate) is True

        # Operator frees disk space ELSEWHERE on the filesystem -- the
        # SAME oracle now reports sufficient headroom. NO directory
        # content changed at all.
        monkeypatch.setattr(
            quarantine_module, "_disk_headroom_currently_sufficient", lambda c: True
        )

        assert is_quarantined(golden_repo_manager, candidate) is False
        assert get_failure_state(golden_repo_manager, candidate.golden_alias) is None

    def test_generic_cause_quarantine_is_unaffected_by_the_disk_headroom_oracle(
        self, tmp_path: Path, sqlite_backend, monkeypatch
    ) -> None:
        import code_indexer.server.services.fleet_migration.quarantine as quarantine_module

        golden_repo_manager = _FakeGoldenRepoManagerWithBackend(sqlite_backend)
        candidate = _make_candidate(tmp_path)
        signature = compute_repo_state_signature(candidate)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            record_migration_failure(
                golden_repo_manager,
                candidate.golden_alias,
                signature,
                failure_cause=GENERIC_FAILURE_CAUSE,
            )

        # Even though the disk-headroom oracle would report "sufficient",
        # a GENERIC-cause quarantine must NOT clear from that alone --
        # only a genuine directory-content change (the signature path)
        # can clear it.
        monkeypatch.setattr(
            quarantine_module, "_disk_headroom_currently_sufficient", lambda c: True
        )

        assert (
            is_quarantined(golden_repo_manager, candidate, recheck_interval_seconds=0)
            is True
        )
