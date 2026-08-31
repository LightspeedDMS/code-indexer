"""Bug #1528: an INCREMENTAL temporal index must never append another
``vector_*.json`` file to a not-yet-migrated shard.

Items 1-2 of this bug make every BRAND-NEW temporal collection a
consolidated ``chunks.db``. That alone leaves one hole: a repo indexed
before the fix already has legacy sharded temporal shards, and an existing
collection's committed on-disk layout always wins (``resolve_chunk_layout``
/ ``_is_chunks_db_collection``), so the next `cidx index --index-commits`
would keep growing the legacy tree.

The fix reuses the migration engine that already exists for exactly this
(``consolidate_collection_in_place``, driven for temporal shards today by
`cidx index --migrate-chunks-to-sqlite`) rather than adding a second
mechanism: before temporal indexing writes anything, any legacy temporal
shard is consolidated in place, under the index-mutation lock the temporal
branch already holds.

Real filesystem, real production writer for the legacy fixture, real
SQLite consolidation.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
from rich.console import Console

from code_indexer import cli as cli_module
from code_indexer.services.chunk_migration_cli import (
    consolidate_legacy_temporal_shards,
    enumerate_migration_targets,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout

SHARD_NAME = "code-indexer-temporal-voyage_code_3-2024Q1"
SEMANTIC_NAME = "code-indexer-voyage-code-3"
VECTOR_SIZE = 8
RNG_SEED = 1528
CHUNK_INDEX = 0
COMMIT_STUB_WIDTH = 8
ROW_IDS = [f"proj:commit:{c * COMMIT_STUB_WIDTH}:{CHUNK_INDEX}" for c in "ab"]

AUTO_CONSOLIDATE_CALL_NAME = "consolidate_legacy_temporal_shards"
#: The temporal indexer construction is the point after which any write can
#: happen, so auto-consolidation must appear strictly before it.
TEMPORAL_INDEXER_CALL_NAME = "TemporalIndexer"


def _points(ids: List[str]) -> List[Dict[str, Any]]:
    # Any: a point record is the store's own heterogeneous public contract
    # (str id, List[float] vector, nested payload dict).
    rng = np.random.default_rng(RNG_SEED)
    return [
        {
            "id": pid,
            "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
            "payload": {"path": f"src/a{i}.py"},
            "chunk_text": f"chunk {i}",
        }
        for i, pid in enumerate(ids)
    ]


def _write_legacy_collection(index_dir: Path, name: str, ids: List[str]) -> Path:
    store = FilesystemVectorStore(
        base_path=index_dir, use_chunks_db_for_new_collections=False
    )
    store.create_collection(name, vector_size=VECTOR_SIZE)
    store.begin_indexing(name)
    store.upsert_points(name, _points(ids))
    store.end_indexing(name)

    collection_dir = index_dir / name
    assert list(collection_dir.rglob("vector_*.json")), "fixture is not legacy-layout"
    return collection_dir


def _run(index_dir: Path) -> Tuple[int, int]:
    # The int() wrap works around this project's pre-existing mypy
    # module-identity quirk (a cross-module call resolves as Any when the
    # source is checked under its src.-prefixed identity), the same
    # workaround collection_migration.py documents for its own callers.
    migrated, failed = consolidate_legacy_temporal_shards(
        index_dir, console=Console(quiet=True)
    )
    return int(migrated), int(failed)


class TestConsolidateLegacyTemporalShards:
    def test_legacy_temporal_shard_is_consolidated_in_place(
        self, tmp_path: Path
    ) -> None:
        index_dir = tmp_path / ".code-indexer" / "index"
        shard_dir = _write_legacy_collection(index_dir, SHARD_NAME, ROW_IDS)

        assert _run(index_dir) == (1, 0)

        assert list(shard_dir.rglob("vector_*.json")) == []
        assert (shard_dir / "chunks.db").is_file()
        assert resolve_chunk_layout(shard_dir) == ChunkLayout.CHUNKS_DB

    def test_rows_survive_and_semantic_collections_are_untouched(
        self, tmp_path: Path
    ) -> None:
        """Only TEMPORAL shards are auto-consolidated: a standalone CLI
        user's SEMANTIC collections keep their explicit-opt-in contract
        (Story #1488)."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _write_legacy_collection(index_dir, SHARD_NAME, ROW_IDS)
        semantic_dir = _write_legacy_collection(index_dir, SEMANTIC_NAME, ["p0", "p1"])

        assert _run(index_dir) == (1, 0)

        assert list(semantic_dir.rglob("vector_*.json")), (
            "a semantic collection must NOT be silently migrated by the "
            "temporal auto-consolidation step"
        )
        assert resolve_chunk_layout(semantic_dir) == ChunkLayout.SHARDED_JSON

        reader = FilesystemVectorStore(base_path=index_dir)
        for pid in ROW_IDS:
            assert reader.get_point(pid, SHARD_NAME) is not None, f"lost row {pid}"

    def test_already_consolidated_and_missing_dirs_are_no_ops(
        self, tmp_path: Path
    ) -> None:
        index_dir = tmp_path / ".code-indexer" / "index"

        # Missing index dir: nothing to do, never raises.
        assert _run(index_dir) == (0, 0)

        index_dir.mkdir(parents=True)
        _write_legacy_collection(index_dir, SHARD_NAME, ROW_IDS)
        assert _run(index_dir) == (1, 0)

        # Second pass: already consolidated, so nothing is migrated again.
        assert _run(index_dir) == (0, 0)


class TestConsolidateLegacyTemporalShardsFailures:
    def test_unmigratable_shard_is_reported_as_a_failure(self, tmp_path: Path) -> None:
        """A shard the engine refuses (here: a corrupt legacy row file) must
        be counted as FAILED and left legacy -- never silently reported as
        migrated, since the caller aborts indexing on any failure."""
        index_dir = tmp_path / ".code-indexer" / "index"
        shard_dir = _write_legacy_collection(index_dir, SHARD_NAME, ROW_IDS)
        corrupt = sorted(shard_dir.rglob("vector_*.json"))[0]
        corrupt.write_text("{ this is not valid json")

        migrated, failed = _run(index_dir)

        assert (migrated, failed) == (0, 1)
        assert resolve_chunk_layout(shard_dir) == ChunkLayout.SHARDED_JSON


class TestMetadataLessTemporalDirectories:
    """Found by a real `cidx index --index-commits` run: ``TemporalIndexer``
    creates a shared bookkeeping directory named ``code-indexer-temporal-
    {slug}`` (the quarter-less shape) that holds NO chunk data and no
    ``collection_meta.json``. It is not a collection and can never be
    consolidated -- the engine cannot even write its metadata -- so it must
    never be enumerated as a migration target. This also broke
    `--migrate-chunks-to-sqlite` on any real temporal repo."""

    def test_bookkeeping_directory_is_not_a_migration_target(
        self, tmp_path: Path
    ) -> None:
        index_dir = tmp_path / ".code-indexer" / "index"
        index_dir.mkdir(parents=True)
        (index_dir / "code-indexer-temporal-voyage_context_4").mkdir()

        assert _run(index_dir) == (0, 0)

    def test_rows_without_metadata_are_reported_as_an_anomaly(
        self, tmp_path: Path
    ) -> None:
        """Never silently skipped and never blindly migrated: a directory
        holding real legacy rows but no metadata is surfaced through the
        existing operator-visible anomaly channel."""
        index_dir = tmp_path / ".code-indexer" / "index"
        orphan = index_dir / "code-indexer-temporal-voyage_context_4-2024Q4"
        shard = orphan / "ab" / "cd"
        shard.mkdir(parents=True)
        (shard / "vector_orphan01.json").write_text("{}")

        inventory = enumerate_migration_targets(index_dir)

        assert orphan not in inventory.temporal
        assert orphan in inventory.anomalies
        assert _run(index_dir) == (0, 0)


def _temporal_branch_nodes() -> List[ast.stmt]:
    tree = ast.parse(inspect.getsource(cli_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "index":
            for inner in ast.walk(node):
                if isinstance(inner, ast.If) and isinstance(inner.test, ast.Name):
                    if inner.test.id == "index_commits":
                        return list(inner.body)
    raise AssertionError(
        "cli.py's `index` command no longer has a bare `if index_commits:` "
        "temporal branch -- this guard has drifted from the real code"
    )


def _first_call_line(nodes: List[ast.stmt], target_name: str) -> Optional[int]:
    lines: Set[int] = set()
    for root in nodes:
        for node in ast.walk(root):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            )
            if name == target_name:
                lines.add(node.lineno)
    return min(lines) if lines else None


def test_cli_temporal_branch_auto_consolidates_before_indexing() -> None:
    """Structural guard: the temporal branch must invoke auto-consolidation,
    and must do so BEFORE the temporal indexer that performs the writes."""
    branch = _temporal_branch_nodes()

    consolidate_line = _first_call_line(branch, AUTO_CONSOLIDATE_CALL_NAME)
    indexer_line = _first_call_line(branch, TEMPORAL_INDEXER_CALL_NAME)

    assert consolidate_line is not None, (
        "cli.py's temporal branch does not call "
        f"{AUTO_CONSOLIDATE_CALL_NAME}(): an incremental `cidx index "
        "--index-commits` against a pre-existing legacy shard would keep "
        "adding vector_*.json files (Bug #1528)"
    )
    assert indexer_line is not None, (
        f"no {TEMPORAL_INDEXER_CALL_NAME}(...) construction found in the "
        "temporal branch -- this guard has drifted from the real code"
    )
    assert consolidate_line < indexer_line, (
        f"{AUTO_CONSOLIDATE_CALL_NAME}() is called at line "
        f"{consolidate_line}, AFTER the temporal indexer is constructed at "
        f"line {indexer_line}: legacy shards must be migrated BEFORE any "
        "temporal write can happen (Bug #1528)"
    )
