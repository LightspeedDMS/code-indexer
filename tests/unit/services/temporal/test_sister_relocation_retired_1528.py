"""Bug #1528 item 4: the Story #1457 sister-location RELOCATION trigger is
retired from the temporal indexing write path.

Why it MUST be unwired rather than left dormant behind its flag: the
publish path sourced its rows exclusively from the legacy hash-sharded
``vector_*.json`` files (``read_legacy_shard_rows``). Now that a temporal
shard is written as a consolidated ``chunks.db`` (items 1-2 of this bug),
that reader finds NOTHING -- so a run with the relocation flag enabled would
build an EMPTY sister version and swap the namespace pointer onto it.
``TemporalShardResolver`` is pointer-first, so every subsequent query for
that namespace would read zero rows: silent data loss on read, not a
degraded-but-correct fallback.

The READ side (``TemporalShardResolver`` + existing alias pointers) is
deliberately left fully intact so any already-published sister data stays
queryable.

Test (a) is a characterization proof of the hazard's mechanism against real
files; test (b) is a structural guard against the write path being re-wired.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from code_indexer.services.temporal import temporal_indexer as temporal_indexer_module
from code_indexer.services.temporal.temporal_row_reader import read_legacy_shard_rows
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

SHARD_NAME = "code-indexer-temporal-voyage_code_3-2024Q1"
VECTOR_SIZE = 8
RNG_SEED = 1528
CHUNK_INDEX = 0
COMMIT_STUB_WIDTH = 8
ROW_IDS = [f"proj:commit:{c * COMMIT_STUB_WIDTH}:{CHUNK_INDEX}" for c in "ab"]

RETIRED_TRIGGER_NAME = "maybe_relocate_shard_to_sister_location"


def _points() -> List[Dict[str, Any]]:
    # Any: a point record is the store's own heterogeneous public contract
    # (str id, List[float] vector, nested payload dict) with no narrower
    # published type.
    rng = np.random.default_rng(RNG_SEED)
    return [
        {
            "id": pid,
            "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
            "payload": {"path": f"src/a{i}.py"},
            "chunk_text": f"chunk {i}",
        }
        for i, pid in enumerate(ROW_IDS)
    ]


def test_legacy_row_reader_sees_nothing_in_a_chunks_db_shard(tmp_path: Path) -> None:
    """The mechanism of the hazard, proven on real files: a fully-populated
    consolidated shard yields ZERO rows to the sister publish path's reader."""
    index_path = tmp_path / "index"
    store = FilesystemVectorStore(
        base_path=index_path, use_chunks_db_for_new_collections=True
    )
    store.create_collection(SHARD_NAME, vector_size=VECTOR_SIZE)
    store.begin_indexing(SHARD_NAME)
    store.upsert_points(SHARD_NAME, _points())
    store.end_indexing(SHARD_NAME)

    shard_dir = index_path / SHARD_NAME
    # Precondition: the rows really are there, in the consolidated store.
    assert (shard_dir / "chunks.db").is_file()
    for pid in ROW_IDS:
        assert store.get_point(pid, SHARD_NAME) is not None

    assert list(read_legacy_shard_rows(shard_dir)) == [], (
        "read_legacy_shard_rows unexpectedly found rows -- update this "
        "characterization test if the reader learned to read chunks.db"
    )


def test_temporal_indexer_no_longer_invokes_the_relocation_trigger() -> None:
    """Structural guard: nothing in temporal_indexer.py may call the retired
    sister-relocation trigger."""
    tree = ast.parse(inspect.getsource(temporal_indexer_module))

    called_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            )
            if name:
                called_names.add(name)

    assert RETIRED_TRIGGER_NAME not in called_names, (
        f"temporal_indexer.py calls {RETIRED_TRIGGER_NAME}(): the "
        f"sister-location publish path sources rows only from legacy "
        f"vector_*.json files, so with chunks.db temporal shards it would "
        f"publish an EMPTY sister version and point queries at it (Bug #1528)"
    )
