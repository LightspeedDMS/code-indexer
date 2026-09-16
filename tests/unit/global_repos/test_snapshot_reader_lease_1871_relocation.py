"""Bug #1871 -- snapshot-reader leases must relocate out of the git-tracked,
semantically-indexed ``cidx-meta`` tree into ``golden-repos/.scratch/``.

Design notes (see the operator's correction comment on issue #1871, which
retracts the issue body's "no migration required" section):

- Writers use ONLY the relocated ``.scratch/snapshot-reader-leases`` root
  from the moment this fix ships -- never the legacy in-tree root, not even
  temporarily. A dual-write compatibility copy into the legacy root was
  considered and rejected: it would put a lease file back inside the
  git-tracked/indexed tree, which is the exact defect this issue exists to
  close (acceptance criterion 1). ``lease_root`` keeps its existing meaning
  (the cidx-meta path every call site already injects); the relocated root
  is derived as ``lease_root.parent / ".scratch" / "snapshot-reader-leases"``
  -- no new injection point, no positional derivation from a snapshot path
  (Bug #1845's fix stays intact).
- Readers (``snapshot_has_live_reader``) check BOTH roots. This closes the
  "old-version node keeps renewing the legacy path after a new-version node
  has moved on" ordering the correction comment names explicitly. The
  reverse ordering (a brand-new lease invisible to an already-running,
  unpatched old-version node's cleanup) is a structural limit of relocating
  shared state during a rolling upgrade of immutable binaries -- not
  something dual-read, or any write-side change that still satisfies AC1,
  can close. This module never writes to the legacy root, so it cannot
  close that direction without violating AC1; documented as an accepted,
  intentional scope boundary.
- The legacy directory's absence is never ambiguous: this fix never
  creates, writes to, or deletes it -- only individually-expired lease
  files within it. The PRIMARY (new) directory's absence IS ambiguous:
  server startup unconditionally creates it before serving traffic, so its
  absence can only mean something removed it out from under a possible
  live lease.

Split across several Write/Edit calls (a few functions at a time) per this
project's per-operation method-count limit.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from code_indexer.global_repos.snapshot_reader_lease import (
    LeaseDirectoryAmbiguousError,
    SnapshotReaderLease,
    _legacy_lease_directory,
    _new_lease_directory,
    ensure_primary_lease_directory,
    snapshot_has_live_reader,
    sweep_expired_lease_files,
)


def test_after_ensure_primary_lease_directory_absence_is_no_longer_ambiguous(
    tmp_path: Path,
) -> None:
    """Bug #1871 follow-up (E2E Phase 4 log-audit gate finding): server
    startup must unconditionally create the PRIMARY (.scratch) lease
    directory before serving traffic -- this module's own
    LeaseDirectoryAmbiguousError docstring already promises exactly this.
    Before this fix, nothing created the directory until the first WRITER
    (a SnapshotReaderLease.acquire()) happened to run, so a fresh server
    with no reader yet raised LeaseDirectoryAmbiguousError and
    cleanup_manager.py deferred cleanup forever -- unbounded snapshot
    accumulation at scale. Once the bootstrap has run, a fresh tree with
    genuinely no reader must resolve to a clean False, not raise."""
    lease_root = _make_lease_root(tmp_path)
    snapshot = _make_snapshot(tmp_path)

    assert not _new_lease_directory(lease_root).exists()

    created = ensure_primary_lease_directory(lease_root)

    assert created == _new_lease_directory(lease_root)
    assert created.is_dir()
    assert snapshot_has_live_reader(str(snapshot), lease_root=lease_root) is False


def test_ensure_primary_lease_directory_is_idempotent(tmp_path: Path) -> None:
    """Startup may run this bootstrap on every boot -- it must never fail
    or duplicate work when the directory already exists from a prior run."""
    lease_root = _make_lease_root(tmp_path)

    first = ensure_primary_lease_directory(lease_root)
    second = ensure_primary_lease_directory(lease_root)

    assert first == second
    assert first.is_dir()


def test_absent_primary_directory_with_no_legacy_evidence_is_ambiguous(
    tmp_path: Path,
) -> None:
    """Bug #1871 TESTS item 3: the primary (.scratch) lease directory's
    absence must never be silently reported as "no live reader" in a way
    that permits snapshot deletion. Server startup unconditionally creates
    this directory before serving traffic, so once neither it nor the
    legacy directory shows any evidence of a reader, the only honest
    conclusion is "unknown" -- not "definitely none". This is the same
    "refuse to guess" posture the module already takes for an unwired
    lease_root; a merely-absent directory carries an identical
    data-loss risk and must be handled identically (raise), not the
    opposite way (silently return False)."""
    lease_root = _make_lease_root(tmp_path)
    snapshot = _make_snapshot(tmp_path)

    assert not _new_lease_directory(lease_root).exists()
    assert not _legacy_lease_directory(lease_root).exists()

    with pytest.raises(LeaseDirectoryAmbiguousError):
        snapshot_has_live_reader(str(snapshot), lease_root=lease_root)


LEASE_TTL_SECONDS = 120.0


def _make_lease_root(tmp_path: Path) -> Path:
    golden_repos = tmp_path / "data" / "golden-repos"
    return golden_repos / "cidx-meta"


def _write_legacy_lease_for_snapshot(
    lease_root: Path, snapshot: Path, age_seconds: float = 0.0
) -> Path:
    """Write a lease file directly into the legacy in-tree directory,
    simulating a pre-#1871 (old-version) node that has no knowledge of the
    relocated root and never goes through this module's resolvers."""
    legacy_directory = _legacy_lease_directory(lease_root)
    legacy_directory.mkdir(parents=True, exist_ok=True)
    path = legacy_directory / "old-node.json"
    path.write_text(
        json.dumps(
            {
                "snapshot_path": str(snapshot.resolve()),
                "lease_id": "old-node",
                "updated_at": time.time() - age_seconds,
                "ttl_seconds": LEASE_TTL_SECONDS,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_legacy_only_lease_from_an_old_version_node_is_still_visible(
    tmp_path: Path,
) -> None:
    """Closes the "old-version writer invisible to new-version cleanup"
    ordering named in the issue #1871 correction comment: a lease written
    directly into the legacy directory (simulating pre-fix code with no
    knowledge of the relocated root) must still be found by new code."""
    lease_root = _make_lease_root(tmp_path)
    snapshot = _make_snapshot(tmp_path)

    _write_legacy_lease_for_snapshot(lease_root, snapshot)

    assert snapshot_has_live_reader(str(snapshot), lease_root=lease_root), (
        "a live lease written directly into the legacy root by a simulated "
        "old-version node must still be visible to new-version code"
    )


def _make_snapshot(tmp_path: Path) -> Path:
    snapshot = tmp_path / "data" / "golden-repos" / "repo" / ".versioned" / "ns" / "v_1"
    snapshot.mkdir(parents=True)
    return snapshot


def test_lease_directory_resolves_outside_the_legacy_cidx_meta_tree(
    tmp_path: Path,
) -> None:
    lease_root = _make_lease_root(tmp_path)
    golden_repos_dir = lease_root.parent

    new_directory = _new_lease_directory(lease_root)
    legacy_directory = _legacy_lease_directory(lease_root)

    assert new_directory == golden_repos_dir / ".scratch" / "snapshot-reader-leases"
    assert legacy_directory == lease_root / ".snapshot-reader-leases"
    assert new_directory != legacy_directory


def test_acquired_lease_file_is_never_created_inside_the_tracked_cidx_meta_tree(
    tmp_path: Path,
) -> None:
    """Acceptance criterion 1: no lease file is ever created inside any
    git-tracked / indexed tree. ``cidx-meta`` (``lease_root`` itself) is
    exactly that tree, so nothing this fix does may create a file under it,
    not even transiently."""
    lease_root = _make_lease_root(tmp_path)
    snapshot = _make_snapshot(tmp_path)

    lease = SnapshotReaderLease(str(snapshot), LEASE_TTL_SECONDS, lease_root=lease_root)
    lease.acquire()
    try:
        lease.renew()

        legacy_files = list(lease_root.rglob("*")) if lease_root.exists() else []
        assert legacy_files == [], (
            "a lease file (or the legacy lease directory itself) was "
            f"created inside the git-tracked cidx-meta tree: {legacy_files!r}"
        )

        new_directory = _new_lease_directory(lease_root)
        assert list(new_directory.glob("*.json")), (
            "expected the lease file to exist under the relocated .scratch root"
        )
    finally:
        lease.release()


def test_cross_node_discovery_still_works_via_the_relocated_shared_root(
    tmp_path: Path,
) -> None:
    """Two independent SnapshotReaderLease objects sharing one lease_root
    (simulating two nodes / two processes) must see each other through the
    relocated root -- cross-node visibility is the entire purpose of the
    lease mechanism and must survive relocation unchanged."""
    lease_root = _make_lease_root(tmp_path)
    snapshot = _make_snapshot(tmp_path)

    lease = SnapshotReaderLease(str(snapshot), LEASE_TTL_SECONDS, lease_root=lease_root)
    lease.acquire()
    try:
        assert snapshot_has_live_reader(str(snapshot), lease_root=lease_root)
    finally:
        lease.release()

    assert not snapshot_has_live_reader(str(snapshot), lease_root=lease_root)


def test_acquire_does_not_overwrite_an_existing_lease_file(tmp_path: Path) -> None:
    """Proves O_CREAT|O_EXCL is still the acquisition primitive at the
    relocated root: a failed second acquire() must not corrupt or replace
    the existing lease file's content -- a weaker test that only checks
    "raises an exception" could also pass for an implementation that
    checked existence first and skipped writing, without ever exercising
    the real atomic-create guarantee."""
    lease_root = _make_lease_root(tmp_path)
    snapshot = _make_snapshot(tmp_path)

    lease = SnapshotReaderLease(str(snapshot), LEASE_TTL_SECONDS, lease_root=lease_root)
    lease.acquire()
    original_content = lease._path.read_text(encoding="utf-8")
    try:
        with pytest.raises(FileExistsError):
            lease.acquire()
        assert lease._path.read_text(encoding="utf-8") == original_content, (
            "a second, colliding acquire() must not modify the existing "
            "lease file's content"
        )
    finally:
        lease.release()


def test_renew_is_atomic_under_concurrent_reads(tmp_path: Path) -> None:
    """Proves renew() still uses an atomic rename (os.replace), not a
    truncate-then-write, by having a background reader continuously parse
    the lease file while renew() is called repeatedly. A non-atomic
    implementation has a real window where the file is empty or partially
    written; os.replace's POSIX rename guarantee has none."""
    lease_root = _make_lease_root(tmp_path)
    snapshot = _make_snapshot(tmp_path)

    lease = SnapshotReaderLease(str(snapshot), LEASE_TTL_SECONDS, lease_root=lease_root)
    lease.acquire()
    stop = threading.Event()
    corrupt_reads: list[str] = []

    def _reader() -> None:
        while not stop.is_set():
            try:
                content = lease._path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            if not content:
                corrupt_reads.append(content)
                continue
            try:
                json.loads(content)
            except json.JSONDecodeError:
                corrupt_reads.append(content)

    reader_thread = threading.Thread(target=_reader)
    reader_thread.start()
    try:
        for _ in range(200):
            lease.renew()
    finally:
        stop.set()
        reader_thread.join(timeout=5.0)
        lease.release()

    assert corrupt_reads == [], (
        f"renew() exposed a non-atomic empty/partial write: {corrupt_reads!r}"
    )


#: Bug #1871 ALSO IN SCOPE item 3: ``sweep_expired_lease_files()`` is a new,
#: pure, stdlib-only helper for the LEGACY directory specifically -- it
#: never touches the primary ``.scratch`` directory, and it never removes
#: the directory itself, only individually-expired files within it (the
#: same, already-safe pattern ``snapshot_has_live_reader`` uses inline).
#: This is the SAFE replacement for the retracted "delete it at startup"
#: plan: `server/startup/lifespan.py` wraps this sync function in
#: ``anyio.to_thread.run_sync`` (mission item 3) so it never blocks the
#: event loop, and calls it against the legacy directory only. It returns
#: `(removed_count, error_count)` -- deliberately not logging anything
#: itself (this module has no logging import and none of its other
#: functions log), leaving the caller (which already has a logger and a
#: correlation id) to emit the "observable success/failure counts" the
#: mission requires.
def _write_arbitrary_lease_file(
    directory: Path, filename: str, *, age_seconds: float, ttl_seconds: float = 120.0
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(
        json.dumps(
            {
                "snapshot_path": "/irrelevant/for/sweep",
                "lease_id": filename,
                "updated_at": time.time() - age_seconds,
                "ttl_seconds": ttl_seconds,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_sweep_expired_lease_files_removes_only_expired_files(tmp_path: Path) -> None:
    directory = tmp_path / "legacy-leases"
    expired = _write_arbitrary_lease_file(
        directory, "expired.json", age_seconds=200.0, ttl_seconds=120.0
    )
    live = _write_arbitrary_lease_file(
        directory, "live.json", age_seconds=1.0, ttl_seconds=120.0
    )

    removed_count, error_count = sweep_expired_lease_files(directory)

    assert removed_count == 1
    assert error_count == 0
    assert not expired.exists(), "an individually-expired lease file must be removed"
    assert live.exists(), "a still-live lease file must never be touched"


def test_sweep_expired_lease_files_never_removes_the_directory_itself(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "legacy-leases"
    _write_arbitrary_lease_file(
        directory, "expired.json", age_seconds=200.0, ttl_seconds=120.0
    )

    sweep_expired_lease_files(directory)

    assert directory.is_dir(), (
        "the sweep must only ever remove individually-expired FILES -- "
        "never the legacy directory itself, or an old-version node still "
        "renewing there loses its ability to recreate a lease file"
    )


def test_sweep_expired_lease_files_is_idempotent_on_repeated_calls(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "legacy-leases"
    _write_arbitrary_lease_file(
        directory, "expired.json", age_seconds=200.0, ttl_seconds=120.0
    )

    first = sweep_expired_lease_files(directory)
    second = sweep_expired_lease_files(directory)

    assert first == (1, 0)
    assert second == (0, 0), (
        "a second sweep over an already-cleaned directory must be a "
        "genuine no-op, not re-count or error on files that are already gone"
    )


def test_sweep_expired_lease_files_on_absent_directory_is_a_safe_no_op(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "never-created"

    removed_count, error_count = sweep_expired_lease_files(directory)

    assert (removed_count, error_count) == (0, 0)
    assert not directory.exists(), "sweeping an absent directory must never create it"


def test_sweep_expired_lease_files_counts_but_does_not_raise_on_a_corrupt_file(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "legacy-leases"
    directory.mkdir(parents=True)
    corrupt = directory / "corrupt.json"
    corrupt.write_text("not valid json {{{", encoding="utf-8")
    expired = _write_arbitrary_lease_file(
        directory, "expired.json", age_seconds=200.0, ttl_seconds=120.0
    )

    removed_count, error_count = sweep_expired_lease_files(directory)

    assert removed_count == 1, (
        "a genuinely expired, well-formed file must still be swept"
    )
    assert error_count == 1, (
        "the corrupt file must be counted as a failure, not silently ignored"
    )
    assert not expired.exists()
    assert corrupt.exists(), (
        "a corrupt file's disposition is unknown (it might be mid-write by "
        "a live process) -- the sweep must never guess-delete it, only "
        "count it as an observable error"
    )
