"""Independent, field-level verification for temporal shard relocation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout
from code_indexer.storage.sqlite_chunk_store import (
    chunk_store_has_real_data,
    open_chunk_store_for_path,
)


class VerificationError(IOError):
    """Raised when source and destination records are not identical."""


def _digest(record: dict[str, Any]) -> str:
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _json_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("vector_*.json")):
        with path.open(encoding="utf-8") as stream:
            record = json.load(stream)
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise VerificationError(f"invalid temporal record: {path}")
        point_id = record["id"]
        if point_id in manifest:
            raise VerificationError(f"duplicate temporal point id: {point_id}")
        manifest[point_id] = _digest(record)
    return manifest


def _chunks_manifest(root: Path) -> dict[str, str]:
    db_path = root / "chunks.db"
    if not chunk_store_has_real_data(db_path, on_error="raise"):
        return {}
    store = open_chunk_store_for_path(db_path, str(root), read_only=True)
    try:
        manifest = {}
        for point_id in sorted(store.all_point_ids()):
            record = store.read(point_id)
            if record is None:
                raise VerificationError(f"chunk point disappeared: {point_id}")
            if point_id in manifest:
                raise VerificationError(f"duplicate temporal point id: {point_id}")
            manifest[point_id] = _digest(record)
        return manifest
    finally:
        store.close()


def _manifest(root: Path) -> dict[str, str]:
    layout = resolve_chunk_layout(root)
    return (
        _chunks_manifest(root)
        if layout is ChunkLayout.CHUNKS_DB
        else _json_manifest(root)
    )


def verify_shard_copy(source: Path, published: Path) -> None:
    """Freshly read every source/destination record and compare all fields."""
    source_manifest = _manifest(source)
    destination_manifest = _manifest(published)
    if source_manifest != destination_manifest:
        raise VerificationError(
            f"temporal shard verification failed: {source} != {published}"
        )


def manifest_digest(root: Path) -> str:
    """Hash of *root*'s full field-for-field manifest.

    Used by ``mover.py`` to write a content-bound provenance marker: unlike
    a bare sentinel file (which any independent process could coincidentally
    or accidentally create), a digest that must match the CURRENT legacy
    source's own manifest digest cannot be satisfied by data that merely
    happens to occupy the same path -- it can only be satisfied by data that
    is field-for-field identical to what was actually verified and
    published.
    """
    manifest = _manifest(root)
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
