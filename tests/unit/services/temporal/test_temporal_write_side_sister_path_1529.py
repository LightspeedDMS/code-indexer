"""Bug #1529 item 2: the temporal WRITE side lands outside the repo tree.

Covers #1529's required tests (a) "a server-context refresh writes to the
fixed sister path, never in-repo" and (e) "standalone CLI is byte-identical
to current behavior", plus a structural guard on the single `cli.py` seam that
decides the location (if that seam regresses to a hardcoded in-repo path, the
whole mechanism goes inert and activations silently start carrying temporal
data again -- the exact half-wiring class of defect #1529 exists to close).

Real filesystem, real SQLite chunk stores, real HNSW builds -- the storage
path is never mocked.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from code_indexer import cli as cli_module
from code_indexer.services.temporal.temporal_server_paths import (
    CIDX_SERVER_REFRESH_CONTEXT_ENV,
    resolve_temporal_index_dir,
    server_temporal_index_root,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

EMBEDDER_SLUG = "voyage_code_3"
QUARTER = "2024Q1"
SHARD_NAME = f"code-indexer-temporal-{EMBEDDER_SLUG}-{QUARTER}"
REPO_ALIAS = "evolution"
VECTOR_SIZE = 8
SEAM_FUNCTION = "resolve_temporal_index_dir"


def _rows() -> List[Dict[str, Any]]:
    rng = np.random.default_rng(1529)
    return [
        {
            "id": f"proj:commit:{'a' * 8}:0",
            "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
            "payload": {"path": "src/auth.py", "commit_hash": "a" * 8},
            "chunk_text": "def authenticate(user): ...",
        }
    ]


def _write_temporal_shard(index_dir: Path) -> Path:
    """Write a REAL consolidated temporal shard, as the post-#1528 write path
    does (chunks.db layout, never legacy vector_*.json)."""
    store = FilesystemVectorStore(
        base_path=index_dir, use_chunks_db_for_new_collections=True
    )
    store.create_collection(SHARD_NAME, vector_size=VECTOR_SIZE)
    store.begin_indexing(SHARD_NAME)
    store.upsert_points(SHARD_NAME, _rows())
    store.end_indexing(SHARD_NAME)
    return index_dir / SHARD_NAME


# ---------------------------------------------------------------------------
# (a) server context writes outside the golden repo's own tree
# ---------------------------------------------------------------------------


def test_server_context_temporal_write_never_touches_the_repo_tree(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    golden_repos_dir = tmp_path / "golden-repos"
    codebase_dir = golden_repos_dir / REPO_ALIAS
    codebase_dir.mkdir(parents=True)

    index_dir = resolve_temporal_index_dir(codebase_dir)
    shard_dir = _write_temporal_shard(index_dir)

    # The data is really there, in the consolidated layout.
    assert (shard_dir / "chunks.db").is_file()
    assert list(shard_dir.rglob("vector_*.json")) == []

    # ...at the fixed sister path...
    assert index_dir == server_temporal_index_root(golden_repos_dir, REPO_ALIAS)

    # ...and the golden repo's OWN tree carries no temporal data at all, so a
    # CoW clone of codebase_dir cannot copy any (#1529 item (b)'s root cause).
    assert not (codebase_dir / ".code-indexer" / "index").exists()
    assert list(codebase_dir.rglob("code-indexer-temporal*")) == []
    assert list(codebase_dir.rglob("chunks.db")) == []


def test_the_fixed_path_is_stable_across_refreshes(tmp_path: Path, monkeypatch) -> None:
    """A second refresh must resolve the SAME directory -- the property that
    keeps the path-derived temporal metadata key from ever going stale (and
    that removes any need for versioning/pointer indirection)."""
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    golden_repos_dir = tmp_path / "golden-repos"
    codebase_dir = golden_repos_dir / REPO_ALIAS
    codebase_dir.mkdir(parents=True)

    first = resolve_temporal_index_dir(codebase_dir)
    _write_temporal_shard(first)
    second = resolve_temporal_index_dir(codebase_dir)

    assert first == second
    # No version directories, no alias pointers -- the whole Story #1457
    # machinery this design replaces.
    assert not (golden_repos_dir / ".versioned").exists()
    assert not (golden_repos_dir / "aliases").exists()


# ---------------------------------------------------------------------------
# (e) standalone CLI byte-identical
# ---------------------------------------------------------------------------


def test_standalone_cli_writes_in_repo_exactly_as_before(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, raising=False)
    codebase_dir = tmp_path / "my-project"
    codebase_dir.mkdir()

    index_dir = resolve_temporal_index_dir(codebase_dir)
    shard_dir = _write_temporal_shard(index_dir)

    assert index_dir == codebase_dir / ".code-indexer" / "index"
    assert (shard_dir / "chunks.db").is_file()
    assert shard_dir.is_relative_to(codebase_dir)


# ---------------------------------------------------------------------------
# Structural guard on the one seam
# ---------------------------------------------------------------------------


def test_cli_temporal_branch_resolves_index_dir_through_the_seam() -> None:
    """cli.py must call the shared seam rather than rebuilding the in-repo
    path inline -- the read side derives its location from the same module,
    so an inline path here would silently desynchronize the two."""
    tree = ast.parse(inspect.getsource(cli_module))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            )
            if name:
                called.add(name)

    assert SEAM_FUNCTION in called, (
        f"cli.py no longer calls {SEAM_FUNCTION}(): the temporal write path "
        f"has regressed to an inline location decision, so server-context "
        f"temporal data will be written inside the golden repo's own tree "
        f"again and every activation will CoW-copy it (Bug #1529)"
    )
