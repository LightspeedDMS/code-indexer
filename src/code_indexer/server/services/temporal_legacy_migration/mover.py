"""Crash-safe per-shard relocation for legacy temporal indexes."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path


_SHARD_PREFIX = "code-indexer-temporal-"


@dataclass(frozen=True)
class MigrationResult:
    published: int = 0
    already_complete: int = 0
    deleted: int = 0
    collisions: int = 0


def _fingerprint(root: Path) -> tuple[tuple[str, str], ...]:
    """Return a deterministic content fingerprint, rejecting symlinks."""
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"temporal migration refuses symlink: {path}")
        if path.is_dir():
            entries.append((relative, "<directory>"))
            continue
        if not path.is_file():
            raise ValueError(f"temporal migration refuses special file: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((relative, digest))
    return tuple(entries)


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        elif path.is_dir():
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _has_data(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


def _publish(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, staging)
        _fsync_tree(staging)
        source_fingerprint = _fingerprint(source)
        target_fingerprint = _fingerprint(staging)
        if source_fingerprint != target_fingerprint:
            raise IOError(f"temporal shard verification failed: {source}")
        if target.exists():
            if _has_data(target):
                raise FileExistsError(f"fixed temporal shard is non-empty: {target}")
            target.rmdir()
        staging.rename(target)
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def migrate_temporal_shards(
    legacy_root: Path,
    fixed_root: Path,
    *,
    relocation_enabled: bool = False,
    cleanup_authorized: bool = False,
) -> MigrationResult:
    """Relocate every legacy shard without changing the legacy source.

    A non-empty fixed shard is authoritative, unconditionally. Cleanup only
    removes a legacy shard after the fixed copy is present and verified.
    """
    if not legacy_root.is_dir():
        return MigrationResult()
    published = already_complete = deleted = collisions = 0
    shards = sorted(
        path
        for path in legacy_root.iterdir()
        if path.name.startswith(_SHARD_PREFIX) and path.is_dir()
    )
    for source in shards:
        target = fixed_root / source.name
        if _has_data(target):
            already_complete += 1
        elif relocation_enabled:
            _publish(source, target)
            published += 1
        if target.is_dir() and _has_data(target) and cleanup_authorized:
            shutil.rmtree(source)
            deleted += 1
    return MigrationResult(published, already_complete, deleted, collisions)
