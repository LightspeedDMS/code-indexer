"""Bug #1979 round 7 (Codex round-6 P2): the maintainer's own acceptance
criterion for this issue is unconditional -- "every configured collection
... uses the CHUNKS_DB layout" after a successful clear=true run. An
explicit `--new-collection-layout=sharded_json` request cannot be honored
together with `--clear` (it would leave legacy `vector_*.json` collections
in place after a "successful" run), so the combination must be rejected
BEFORE any indexing, mirroring the existing `--clear` + `--rebuild-*`
conflict guard in `test_cli_clear_rebuild_flags_conflict_1979.py`.

`--clear --new-collection-layout=chunks_db` and plain `--clear` remain
allowed (proven by the sibling `test_cli_clear_default_chunks_db_1979.py`
and `test_cli_clear_recovers_damaged_layout_1979.py`).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout


def test_cli_rejects_clear_with_explicit_sharded_json_layout(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "example-repo"
    repo.mkdir()
    (repo / "example.py").write_text("def example():\n    return 1\n")

    child_bootstrap = tmp_path / "child-bootstrap"
    child_bootstrap.mkdir()
    (child_bootstrap / "sitecustomize.py").write_text(
        "from code_indexer.services.voyage_ai import VoyageAIClient\n"
        "def fake_text_batch(self, texts, model=None, *, embedding_purpose='document', retry=True):\n"
        "    return [[1.0] + [0.0] * (self.get_model_info()['dimensions'] - 1) for _ in texts]\n"
        "VoyageAIClient.get_embeddings_batch = fake_text_batch\n"
    )
    project_src = Path(__file__).resolve().parents[3] / "src"
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join((str(child_bootstrap), str(project_src)))
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "example-key")
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server-data"))
    child_env = os.environ.copy()

    initialized = subprocess.run(
        ["cidx", "init"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert initialized.returncode == 0, initialized.stderr or initialized.stdout
    indexed = subprocess.run(
        ["cidx", "index"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert indexed.returncode == 0, indexed.stderr or indexed.stdout

    index_root = repo / ".code-indexer" / "index"
    collections_before = [path for path in index_root.iterdir() if path.is_dir()]
    assert collections_before
    store_before = FilesystemVectorStore(index_root, project_root=repo)
    layouts_before = {
        collection.name: resolve_chunk_layout(collection)
        for collection in collections_before
    }
    points_before = {
        collection.name: store_before.count_points(collection.name)
        for collection in collections_before
    }

    result = subprocess.run(
        ["cidx", "index", "--clear", "--new-collection-layout", "sharded_json"],
        cwd=repo,
        env=child_env,
        text=True,
        capture_output=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "--clear" in output and "sharded_json" in output, output
    assert "Cannot use" in output, output

    # Rejected BEFORE any indexing: the pre-existing collection is untouched.
    collections_after = [path for path in index_root.iterdir() if path.is_dir()]
    # Codex round-7 P3: no collection was created or deleted either -- not
    # just each pre-existing one's layout/points left unchanged.
    assert {c.name for c in collections_before} == {
        c.name for c in collections_after
    }, "collection set changed despite the rejected --clear"
    store_after = FilesystemVectorStore(index_root, project_root=repo)
    for collection in collections_after:
        assert resolve_chunk_layout(collection) == layouts_before[collection.name], (
            f"{collection.name} layout changed despite the rejected --clear"
        )
        assert (
            store_after.count_points(collection.name) == points_before[collection.name]
        ), f"{collection.name} points changed despite the rejected --clear"


def test_cli_allows_clear_with_explicit_chunks_db_layout(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: --clear --new-collection-layout=chunks_db must remain
    allowed -- only the legacy sharded_json combination is rejected."""
    repo = tmp_path / "example-repo"
    repo.mkdir()
    (repo / "example.py").write_text("def example():\n    return 1\n")

    child_bootstrap = tmp_path / "child-bootstrap"
    child_bootstrap.mkdir()
    (child_bootstrap / "sitecustomize.py").write_text(
        "from code_indexer.services.voyage_ai import VoyageAIClient\n"
        "def fake_text_batch(self, texts, model=None, *, embedding_purpose='document', retry=True):\n"
        "    return [[1.0] + [0.0] * (self.get_model_info()['dimensions'] - 1) for _ in texts]\n"
        "VoyageAIClient.get_embeddings_batch = fake_text_batch\n"
    )
    project_src = Path(__file__).resolve().parents[3] / "src"
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join((str(child_bootstrap), str(project_src)))
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "example-key")
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server-data"))
    child_env = os.environ.copy()

    initialized = subprocess.run(
        ["cidx", "init"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert initialized.returncode == 0, initialized.stderr or initialized.stdout
    indexed = subprocess.run(
        ["cidx", "index"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert indexed.returncode == 0, indexed.stderr or indexed.stdout

    result = subprocess.run(
        ["cidx", "index", "--clear", "--new-collection-layout", "chunks_db"],
        cwd=repo,
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    index_root = repo / ".code-indexer" / "index"
    for collection in index_root.iterdir():
        if collection.is_dir():
            assert resolve_chunk_layout(collection) == ChunkLayout.CHUNKS_DB
