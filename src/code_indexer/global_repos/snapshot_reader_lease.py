"""Cluster-visible leases for long-lived snapshot readers."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Tuple

SNAPSHOT_READER_LEASE_TTL_SECONDS = 120.0


class LeaseDirectoryAmbiguousError(RuntimeError):
    """Raised when the PRIMARY lease directory is unexpectedly absent.

    Bug #1871: absence of the relocated ``.scratch`` lease directory is
    never treated as "definitely no live reader" -- that silent-False
    behaviour is exactly what let a rolling-upgrade race delete a snapshot
    out from under a live reader (retracted "delete it at startup" plan,
    issue #1871 correction comment). Server startup unconditionally creates
    this directory before serving traffic, so once a server has booted at
    least once post-fix, its absence can only mean external interference
    with a directory that may hold a live lease. Callers must treat this
    identically to "live reader found" (defer), never as permission to
    proceed -- symmetric with this module's pre-existing unwired-
    ``lease_root`` raise.
    """


def _new_lease_directory(lease_root: Path) -> Path:
    """The Bug #1871 relocated lease directory: golden-repos/.scratch/....

    ``lease_root`` keeps the meaning every existing call site already
    injects (``get_cidx_meta_path(server_dir)``, i.e.
    ``golden_repos_dir / "cidx-meta"``), so no call site needs to change to
    get the relocated behaviour: ``golden_repos_dir`` is derived as
    ``lease_root.parent`` -- still no positional derivation from a
    snapshot path (Bug #1845's fix stays intact).
    """
    return Path(lease_root).parent / ".scratch" / "snapshot-reader-leases"


def ensure_primary_lease_directory(lease_root: Path) -> Path:
    """Idempotently create the PRIMARY (relocated ``.scratch``) lease
    directory and return its path.

    Bug #1871 follow-up (caught by the E2E Phase 4 post-run log-audit
    gate): this module's own ``LeaseDirectoryAmbiguousError`` docstring
    already promises that "server startup unconditionally creates this
    directory before serving traffic" -- but nothing actually did. Before
    this function existed, the PRIMARY directory was only ever created as
    a side effect of a WRITER taking a lease (``_lease_directory(...,
    create=True)`` inside ``SnapshotReaderLease.__init__``). On a fresh
    server where no reader has yet taken a lease, ``snapshot_has_live_
    reader()`` found the directory absent and raised
    ``LeaseDirectoryAmbiguousError``; ``cleanup_manager.py`` caught that
    and deferred cleanup indefinitely, so snapshots accumulated without
    bound at the project's ~900-repo production scale.

    Callers must invoke this from server startup so the directory's later
    absence is genuinely anomalous, exactly as the docstring above
    assumes. This is a synchronous, stdlib-only ``mkdir`` -- callers on an
    async startup path must offload it via ``anyio.to_thread.run_sync``,
    identically to this module's sibling ``sweep_expired_lease_files``.
    """
    directory = _new_lease_directory(lease_root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _legacy_lease_directory(lease_root: Path) -> Path:
    """The pre-#1871 lease directory, inside the git-tracked cidx-meta tree.

    Never written to by this module as of Bug #1871 -- kept read-only so an
    old-version node's lease (written by pre-fix code with no knowledge of
    the relocated root) stays visible to new-version readers during a
    rolling upgrade. Nothing here creates, writes to, or deletes this
    directory itself; only individually-expired files within it are ever
    removed (see ``snapshot_has_live_reader`` and
    ``sweep_expired_lease_files``).
    """
    return Path(lease_root) / ".snapshot-reader-leases"


def _lease_directory(
    snapshot_path: str, *, lease_root: Path, create: bool = True
) -> Path:
    """Resolve the shared PRIMARY lease directory under an explicitly
    injected root.

    Bug #1845 remediation round 2 (Defect 3): the previous implementation
    derived this directory positionally (``path.parents[2]``), which on a
    real deployment lands one level short of ``golden_repos_dir`` and
    creates ``cidx-meta/`` *inside* the golden repo tree, sibling to
    ``.versioned``. There is no positional derivation that is safe for
    every valid snapshot-path depth, so the caller must resolve and inject
    the real shared ``cidx-meta`` root explicitly.

    Bug #1871: the directory this resolves to is now the RELOCATED root
    (``_new_lease_directory``), not the legacy in-tree one -- see that
    function's docstring and this module's top-level design notes.
    ``snapshot_path`` is intentionally unused for the directory itself
    (identical for every snapshot under one lease_root); it stays a
    parameter so ``_snapshot_key`` derivation stays visually paired with
    directory resolution at call sites.
    """
    if lease_root is None:
        raise ValueError(
            "_lease_directory requires an explicit lease_root -- the "
            "caller must resolve the shared cidx-meta directory before "
            "locating a lease. No positional derivation, no fallback "
            "(Bug #1845 remediation round 2, Defect 3)."
        )
    directory = _new_lease_directory(lease_root)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def _snapshot_key(snapshot_path: str) -> str:
    return hashlib.sha256(str(Path(snapshot_path).resolve()).encode()).hexdigest()


class SnapshotReaderLease:
    """A renewable, per-reader lease for one snapshot path."""

    def __init__(
        self, snapshot_path: str, ttl_seconds: float, *, lease_root: Path
    ) -> None:
        self.snapshot_path = str(Path(snapshot_path).resolve())
        self._directory = _lease_directory(self.snapshot_path, lease_root=lease_root)
        self._lease_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex}"
        self._path = self._directory / (
            f"{_snapshot_key(self.snapshot_path)}-{self._lease_id}.json"
        )
        self._released = False
        self._ttl_seconds = ttl_seconds

    def acquire(self) -> None:
        payload = {
            "snapshot_path": self.snapshot_path,
            "lease_id": self._lease_id,
            "updated_at": time.time(),
            "ttl_seconds": self._ttl_seconds,
        }
        fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, json.dumps(payload).encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def renew(self) -> None:
        if self._released:
            return
        payload = {
            "snapshot_path": self.snapshot_path,
            "lease_id": self._lease_id,
            "updated_at": time.time(),
            "ttl_seconds": self._ttl_seconds,
        }
        temporary = self._path.with_suffix(f".tmp.{uuid.uuid4().hex}")
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(str(temporary), str(self._path))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def release(self) -> None:
        self._released = True
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass


def snapshot_has_live_reader(snapshot_path: str, *, lease_root: Path) -> bool:
    """Return whether any non-expired reader lease exists for the snapshot.

    Bug #1845 remediation round 2 (Defect 1): classifying ``snapshot_path``
    as a real versioned snapshot is now the CALLER's responsibility (e.g.
    via an already-wired ``VersionedSnapshotManager`` facade), not this
    function's -- the previous local ``is_versioned_snapshot_path``
    substring-test duplicate is deleted. Importing the canonical
    ``is_versioned_snapshot`` from ``server/storage/shared/snapshot_paths``
    directly into this module was measured to pull in 214 additional
    modules (vs. this module's own ~60-module baseline import footprint),
    including telemetry/OTEL, JWT secret management and NFS/ONTAP backend
    code -- a proven instance of the same CLI-layering regression class
    Bug #1467/#1468 exist to prevent, since this module is transitively
    CLI-reachable via ``cleanup_manager.py``. Direct import was therefore
    rejected in favor of caller-side classification.
    """
    prefix = f"{_snapshot_key(snapshot_path)}-"
    now = time.time()

    def scan(directory: Path, pattern: str) -> bool:
        if not directory.is_dir():
            return False
        for lease_path in directory.glob(pattern):
            try:
                data = json.loads(lease_path.read_text(encoding="utf-8"))
                if data.get("snapshot_path") != str(Path(snapshot_path).resolve()):
                    continue
                age = now - float(data["updated_at"])
                ttl_seconds = float(
                    data.get("ttl_seconds", SNAPSHOT_READER_LEASE_TTL_SECONDS)
                )
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            if age <= ttl_seconds:
                return True
            try:
                lease_path.unlink()
            except FileNotFoundError:
                pass
        return False

    # During rolling upgrades, old nodes may continue renewing the legacy
    # in-tree lease. Check it first so its liveness is never hidden by the
    # relocated directory's state.
    if scan(_legacy_lease_directory(lease_root), "*.json"):
        return True

    directory = _lease_directory(snapshot_path, lease_root=lease_root, create=False)
    if not directory.is_dir():
        raise LeaseDirectoryAmbiguousError(
            "primary snapshot-reader lease directory is absent; refusing "
            "to classify the snapshot as reader-free during migration"
        )
    return scan(directory, f"{prefix}*.json")


def sweep_expired_lease_files(directory: Path) -> Tuple[int, int]:
    """Remove expired lease files without removing the lease directory.

    This is a synchronous, stdlib-only primitive intended to run in a worker
    thread when called from the server startup path.  Missing directories are
    a safe no-op: an old-version node may still recreate the legacy directory
    during a rolling upgrade.  Files that cannot be parsed or inspected are
    counted as errors and left in place; only a positively identified expired
    lease is eligible for removal.

    Returns ``(removed_count, error_count)``.
    """
    if not directory.is_dir():
        return 0, 0

    now = time.time()
    removed_count = 0
    error_count = 0
    for lease_path in directory.glob("*.json"):
        try:
            data = json.loads(lease_path.read_text(encoding="utf-8"))
            age = now - float(data["updated_at"])
            ttl_seconds = float(
                data.get("ttl_seconds", SNAPSHOT_READER_LEASE_TTL_SECONDS)
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            error_count += 1
            continue

        if age <= ttl_seconds:
            continue
        try:
            lease_path.unlink()
        except FileNotFoundError:
            # A concurrent reader released or replaced it; that is benign.
            continue
        except OSError:
            error_count += 1
            continue
        removed_count += 1

    return removed_count, error_count
