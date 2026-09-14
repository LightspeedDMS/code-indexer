"""Cluster-visible leases for long-lived snapshot readers."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
from pathlib import Path

SNAPSHOT_READER_LEASE_TTL_SECONDS = 120.0


def _lease_directory(
    snapshot_path: str, *, lease_root: Path, create: bool = True
) -> Path:
    """Resolve the shared lease directory under an explicitly injected root.

    Bug #1845 remediation round 2 (Defect 3): the previous implementation
    derived this directory positionally (``path.parents[2]``), which on a
    real deployment lands one level short of ``golden_repos_dir`` and
    creates ``cidx-meta/`` *inside* the golden repo tree, sibling to
    ``.versioned``. There is no positional derivation that is safe for
    every valid snapshot-path depth, so the caller must resolve and inject
    the real shared ``cidx-meta`` root explicitly (e.g. via
    ``golden_repos_dir / "cidx-meta"``, which is byte-identical to what
    ``get_cidx_meta_path(server_data_dir)`` would compute, since
    ``golden_repos_dir == server_data_dir / "data" / "golden-repos"``).
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
    directory = Path(lease_root) / ".snapshot-reader-leases"
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
    directory = _lease_directory(snapshot_path, lease_root=lease_root, create=False)
    if not directory.is_dir():
        return False
    prefix = f"{_snapshot_key(snapshot_path)}-"
    now = time.time()
    live = False
    for lease_path in directory.glob(f"{prefix}*.json"):
        try:
            data = json.loads(lease_path.read_text(encoding="utf-8"))
            age = now - float(data["updated_at"])
            ttl_seconds = float(
                data.get("ttl_seconds", SNAPSHOT_READER_LEASE_TTL_SECONDS)
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if age <= ttl_seconds:
            live = True
        else:
            try:
                lease_path.unlink()
            except FileNotFoundError:
                pass
    return live
