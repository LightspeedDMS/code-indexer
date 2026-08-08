"""Verification primitives shared by migration and health surfaces."""

from pathlib import Path

from .mover import _fingerprint


def verify_shard_copy(source: Path, published: Path) -> None:
    """Freshly read both shards and fail loudly if their contents differ."""
    if _fingerprint(source) != _fingerprint(published):
        raise IOError(f"temporal shard verification failed: {source} != {published}")
