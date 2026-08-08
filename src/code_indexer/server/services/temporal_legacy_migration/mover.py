"""Crash-safe per-shard relocation for legacy temporal indexes."""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .verification import verify_shard_copy


_SHARD_PREFIX = "code-indexer-temporal-"


@dataclass(frozen=True)
class MigrationResult:
    published: int = 0
    already_complete: int = 0
    deleted: int = 0
    collisions: int = 0


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
        verify_shard_copy(source, staging)
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
    metadata_backend_factory: Optional[Callable[[Path], object]] = None,
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
            if metadata_backend_factory is not None:
                metadata_backend_factory(source).copy_collection_scope(target)  # type: ignore[attr-defined]
            published += 1
        if target.is_dir() and _has_data(target) and cleanup_authorized:
            shutil.rmtree(source)
            if metadata_backend_factory is not None:
                metadata_backend_factory(source).delete_collection_scope()  # type: ignore[attr-defined]
            deleted += 1
    return MigrationResult(published, already_complete, deleted, collisions)
