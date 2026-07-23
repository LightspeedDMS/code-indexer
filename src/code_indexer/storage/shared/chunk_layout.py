"""Shared chunk-storage layout discriminator + resolver (Epic #1454).

This module is the SOLE authority for deciding whether a collection
directory uses the legacy sharded ``vector_*.json`` layout or the new
consolidated ``chunks.db`` layout (Story #1456, "Semantic Index
Consolidation"). It is intentionally minimal: it owns only the discriminator
schema and the fail-closed resolver primitive. It does NOT own fleet
migration (backup/verify/flip) -- that is Story #1458's responsibility, and
it will import and reuse THIS module rather than reimplementing the
discriminator.

Discriminator schema
---------------------
A ``chunks_db`` key lives at the TOP LEVEL of ``collection_meta.json``, as a
sibling of the existing ``metadata``/``hnsw_index`` keys -- never nested
inside them (those remain completely untouched by this module, per AC1)::

    {
        "name": "my_collection",
        "vector_size": 1024,
        "metadata": {"hnsw_index": {"id_mapping": {...}}},
        "chunks_db": {"enabled": true, "version": 1}
    }

Fail-closed contract
---------------------
``resolve_chunk_layout`` NEVER raises and NEVER guesses. Any of the
following resolve to ``ChunkLayout.SHARDED_JSON``:

- ``collection_meta.json`` missing, empty, not valid JSON, or not
  decodable as UTF-8 text.
- The top-level JSON value is not an object (dict).
- The ``chunks_db`` key is absent.
- The ``chunks_db`` value is not an object, or ``enabled`` is not
  ``True`` (a bool, not merely truthy), or ``enabled`` is missing/wrong type.
- Any OSError while reading (permission error, path is a file not a
  directory, etc.).

Only a well-formed, explicitly-``enabled: true`` discriminator resolves to
``ChunkLayout.CHUNKS_DB``.

Durability
----------
``collection_meta.json`` holds the load-bearing HNSW integer-label ->
point_id bridge (``metadata["hnsw_index"]["id_mapping"]``). Any write to this
file MUST be atomic+durable (temp file + fsync + ``os.replace`` + directory
fsync) -- a bare ``write_text()`` risks destroying that bridge on a
mid-write crash, which would be strictly worse than this module's
fail-closed contract. ``write_chunks_db_discriminator`` reuses the SAME
pattern this repo already established for this exact file:
``HNSWIndexManager._atomic_write_metadata_durable`` (Bug #1407,
``hnsw_index_manager.py``).
"""

from __future__ import annotations

import json
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Union

from code_indexer.utils.file_locking import nfs_safe_fsync

#: Current discriminator schema version. Bump only on a genuine breaking
#: change to the ``chunks_db`` sub-object shape.
CHUNK_LAYOUT_DISCRIMINATOR_VERSION = 1

_COLLECTION_META_FILENAME = "collection_meta.json"
_DISCRIMINATOR_KEY = "chunks_db"


class ChunkLayout(Enum):
    """Which on-disk layout a collection's chunk data uses."""

    SHARDED_JSON = "sharded_json"
    CHUNKS_DB = "chunks_db"


def resolve_chunk_layout(collection_dir: Union[str, Path]) -> ChunkLayout:
    """Return the :class:`ChunkLayout` for ``collection_dir``.

    Fails CLOSED to ``ChunkLayout.SHARDED_JSON`` on any absent, malformed, or
    invalid discriminator -- this function never raises and never guesses.
    This is the ONLY function in the codebase authorized to make this
    decision; callers must never independently probe for ``chunks.db``'s
    existence or hand-check the ``chunks_db`` field.
    """
    meta_path = Path(collection_dir) / _COLLECTION_META_FILENAME

    try:
        content = meta_path.read_text()
    except (OSError, UnicodeDecodeError):
        return ChunkLayout.SHARDED_JSON

    if not content.strip():
        return ChunkLayout.SHARDED_JSON

    try:
        meta = json.loads(content)
    except json.JSONDecodeError:
        return ChunkLayout.SHARDED_JSON

    if not isinstance(meta, dict):
        return ChunkLayout.SHARDED_JSON

    discriminator = meta.get(_DISCRIMINATOR_KEY)
    if not isinstance(discriminator, dict):
        return ChunkLayout.SHARDED_JSON

    if discriminator.get("enabled") is True:
        return ChunkLayout.CHUNKS_DB

    return ChunkLayout.SHARDED_JSON


def _atomic_write_json_durable(target_path: Path, data: dict) -> None:
    """Write ``data`` as JSON to ``target_path`` atomically and durably.

    Identical pattern to ``HNSWIndexManager._atomic_write_metadata_durable``
    (Bug #1407, ``hnsw_index_manager.py``): write to a sibling temp file in
    the SAME directory, flush+fsync it, ``os.replace`` it into place, then
    fsync the containing directory so the rename itself survives a
    crash/power-loss. On any failure, the temp file is best-effort cleaned
    up before the original exception propagates.
    """
    collection_dir = target_path.parent
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(collection_dir), suffix=".tmp")
    fd_owned = False
    try:
        try:
            tmp_f = os.fdopen(tmp_fd, "w")
            fd_owned = True
            with tmp_f:
                json.dump(data, tmp_f)
                tmp_f.flush()
                nfs_safe_fsync(tmp_f.fileno())
            os.replace(tmp_path, str(target_path))
        finally:
            if not fd_owned:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass  # Already closed or invalid -- discard
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass  # Best-effort cleanup -- discard so original exception propagates
        raise

    dir_fd = os.open(str(collection_dir), os.O_RDONLY)
    try:
        nfs_safe_fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_chunks_db_discriminator(collection_dir: Union[str, Path]) -> None:
    """Durably commit the ``chunks_db`` discriminator into an EXISTING
    ``collection_meta.json`` inside ``collection_dir``.

    This is a mandatory FINAL step in the fresh-collection build path (AC1):
    callers MUST invoke this only AFTER the ``chunks.db`` chunk store and all
    of its indexes (HNSW, path_index) are fully written and durable. Writing
    this flag before the store it points to exists/is complete would make a
    partially-built collection appear as a valid CHUNKS_DB collection.

    The write itself is atomic+durable (temp file + fsync + ``os.replace`` +
    directory fsync) -- see :func:`_atomic_write_json_durable`. This file
    holds the load-bearing HNSW ``id_mapping``; a bare, non-atomic write
    could destroy it on a mid-write crash.

    Raises ``FileNotFoundError`` if ``collection_meta.json`` does not already
    exist -- this function never creates one from scratch, so a caller that
    invokes it out of order (before the base metadata write) fails loudly
    instead of masking the ordering bug.
    """
    meta_path = Path(collection_dir) / _COLLECTION_META_FILENAME
    if not meta_path.exists():
        raise FileNotFoundError(
            f"Cannot write chunks_db discriminator: {meta_path} does not "
            f"exist. write_chunks_db_discriminator() must be called AFTER "
            f"the base collection_meta.json has already been written."
        )

    meta = json.loads(meta_path.read_text())
    meta[_DISCRIMINATOR_KEY] = {
        "enabled": True,
        "version": CHUNK_LAYOUT_DISCRIMINATOR_VERSION,
    }
    _atomic_write_json_durable(meta_path, meta)
