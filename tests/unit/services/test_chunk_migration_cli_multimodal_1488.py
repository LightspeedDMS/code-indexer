"""Story #1488 (Codex Finding F): the standalone
`cidx index --migrate-chunks-to-sqlite` orchestration must ALSO migrate
nested LEGACY multimodal collections.

Real legacy multimodal collections live one level deeper than an ordinary
semantic collection: under ``.code-indexer/index/multimodal_index/<collection>/``
(the ``multimodal_index`` CONTAINER directory itself has NO direct
``collection_meta.json``). The container is created by
``FilesystemVectorStore(..., subdirectory="multimodal_index")`` whose
``base_path`` is ``.code-indexer/index`` (see
``services/multi_index_query_service.py`` and
``tests/unit/storage/test_filesystem_vector_store_subdirectory.py``).

Before the fix, ``enumerate_migration_targets`` examined only the IMMEDIATE
children of ``.code-indexer/index/`` -- so the ``multimodal_index`` container
(no direct ``collection_meta.json`` but nested ``vector_*.json`` found via
``rglob``) was classified as an UNRECOGNIZED directory and its child
collections were NEVER migrated. This test proves the container is descended,
its child collections migrated, and the container itself is neither
migrated-as-a-collection nor mis-reported.

Real files, REAL SQLite (the real ``ChunkStore``), REAL sockets and REAL
fcntl locks -- no mocking of the code under test.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from rich.console import Console

from code_indexer.services.chunk_migration_cli import (
    enumerate_migration_targets,
    run_chunk_migration,
)
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)


# --------------------------------------------------------------------------
# Helpers (mirror the real FilesystemVectorStore sharded record shape, reused
# verbatim from test_chunk_migration_cli_1488.py's own helpers).
# --------------------------------------------------------------------------
def _write_vector_json(
    collection_dir: Path,
    point_id: str,
    vector,
    *,
    path: str = "docs/guide.md",
    chunk_text: Optional[str] = None,
) -> Path:
    payload = {"path": path, "language": "markdown"}
    record = {
        "id": point_id,
        "vector": vector,
        "metadata": {"language": "markdown"},
        "payload": payload,
    }
    if chunk_text is not None:
        record["chunk_text"] = chunk_text
        record["indexed_with_uncommitted_changes"] = True
    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(collection_dir: Path, vector_size: int = 4) -> None:
    collection_dir.mkdir(parents=True, exist_ok=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": collection_dir.name, "vector_size": vector_size})
    )


def _make_sharded_collection(parent: Path, name: str, n: int = 2) -> Path:
    coll = parent / name
    _write_collection_meta(coll)
    for i in range(n):
        _write_vector_json(coll, f"mm{i:06d}", [float(i)] * 4, chunk_text=f"c-{i}")
    return coll


def _config(codebase_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(codebase_dir=codebase_dir)


class _FakeConfigManager:
    """Minimal config-manager double exposing ONLY get_socket_path(). Points
    at a non-existent socket by default (no live daemon)."""

    def __init__(self, socket_path: Path):
        self._socket_path = socket_path

    def get_socket_path(self) -> Path:
        return self._socket_path


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=120), buf


# --------------------------------------------------------------------------
# AC5 (Codex Finding F): enumeration descends the multimodal_index container
# --------------------------------------------------------------------------
class TestMultimodalEnumeration:
    def test_nested_multimodal_collection_discovered_as_semantic(
        self, tmp_path: Path
    ) -> None:
        index_dir = tmp_path / "index"
        index_dir.mkdir()
        # A normal top-level semantic collection.
        top = _make_sharded_collection(index_dir, "code-index-abc")
        # A LEGACY nested multimodal collection under multimodal_index/.
        container = index_dir / "multimodal_index"
        mm = _make_sharded_collection(container, "voyage-multimodal-3")

        inv = enumerate_migration_targets(index_dir)

        semantic_paths = set(inv.semantic)
        # The nested multimodal collection IS discovered as a migration target.
        assert mm in semantic_paths, (
            "nested multimodal collection must be discovered as a migration target"
        )
        # The ordinary top-level collection is still discovered.
        assert top in semantic_paths
        # The container itself is NEVER a migratable collection...
        assert container not in semantic_paths
        assert container not in set(inv.temporal)
        # ...and is NEVER reported as unrecognized (even though it holds
        # nested vector_*.json data).
        assert container not in set(inv.unrecognized)
        assert inv.unrecognized == []

    def test_empty_multimodal_container_is_silently_skipped(
        self, tmp_path: Path
    ) -> None:
        index_dir = tmp_path / "index"
        index_dir.mkdir()
        (index_dir / "multimodal_index").mkdir()  # empty container

        inv = enumerate_migration_targets(index_dir)

        assert inv.semantic == []
        assert inv.temporal == []
        assert inv.unrecognized == []
        assert inv.anomalies == []


# --------------------------------------------------------------------------
# AC3/AC4/AC8/AC9: nested multimodal collection is actually migrated
# --------------------------------------------------------------------------
class TestMultimodalMigration:
    def _wire(self, tmp_path: Path):
        codebase = tmp_path / "repo"
        index_dir = codebase / ".code-indexer" / "index"
        index_dir.mkdir(parents=True)
        cm = _FakeConfigManager(tmp_path / "daemon.sock")
        return codebase, index_dir, cm

    def test_nested_multimodal_collection_migrated_exit_zero(
        self, tmp_path: Path
    ) -> None:
        codebase, index_dir, cm = self._wire(tmp_path)
        container = index_dir / "multimodal_index"
        mm = _make_sharded_collection(container, "voyage-multimodal-3", n=3)
        console, buf = _console()

        exit_code = run_chunk_migration(_config(codebase), cm, console=console)

        assert exit_code == 0
        # Nested multimodal collection consolidated in place.
        assert resolve_chunk_layout(mm) == ChunkLayout.CHUNKS_DB
        assert (mm / "chunks.db").exists()
        assert next(mm.rglob("vector_*.json"), None) is None
        # The container itself is NOT migrated as a collection: no chunks.db,
        # no discriminator, no collection_meta.json at the container level.
        assert not (container / "chunks.db").exists()
        assert not (container / "collection_meta.json").exists()
        # Status table printed and reports the nested collection migrated.
        out = buf.getvalue().lower()
        assert "migrated" in out
        # The container is NOT surfaced as an unrecognized/anomaly directory.
        assert "unrecognized" not in out
