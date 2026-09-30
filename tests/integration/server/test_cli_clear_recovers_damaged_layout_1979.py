"""Bug #1979 step 2: an explicit CHUNKS_DB `--clear` must recover a
collection that is already on the legacy SHARDED_JSON layout (the state
left behind by the pre-fix activated-repo clear bug, or simply a repo
that has always used the CLI's default legacy layout).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout


def test_explicit_chunks_db_clear_recovers_sharded_json_collection(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "example-repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def greet(name: str) -> str:\n"
        "    return f'Hello, {name}'\n"
        "\n"
        "def farewell(name: str) -> str:\n"
        "    return f'Goodbye, {name}'\n"
    )

    # Bug #1979 mission Definition of Done: "the test embedder is fine" --
    # only the external paid Voyage HTTP call is substituted here. The CLI,
    # SmartIndexer, and FilesystemVectorStore under test are all real and
    # unmocked. Matches the identical pattern already used in the sibling
    # test tests/integration/server/test_activated_semantic_clear_1979.py.
    child_bootstrap = tmp_path / "child-bootstrap"
    child_bootstrap.mkdir()
    (child_bootstrap / "sitecustomize.py").write_text(
        "from code_indexer.services.voyage_ai import VoyageAIClient\n"
        "def fake_embeddings(self, texts, model=None, *, embedding_purpose='document', retry=True):\n"
        "    return [[1.0] + [0.0] * 1023 for _ in texts]\n"
        "VoyageAIClient.get_embeddings_batch = fake_embeddings\n"
    )
    project_src = Path(__file__).resolve().parents[3] / "src"
    child_pythonpath = os.pathsep.join((str(child_bootstrap), str(project_src)))
    monkeypatch.setenv("PYTHONPATH", child_pythonpath)
    monkeypatch.setenv("VOYAGE_API_KEY", "example-key")
    child_env = os.environ.copy()

    initialized = subprocess.run(
        ["cidx", "init"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert initialized.returncode == 0, initialized.stderr or initialized.stdout

    # Plain `cidx index` with no --new-collection-layout flag: this is the
    # CLI's documented default (SHARDED_JSON) -- the same on-disk state a
    # repo damaged by the pre-fix activated-repo clear bug is left in.
    first_index = subprocess.run(
        ["cidx", "index"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert first_index.returncode == 0, first_index.stderr or first_index.stdout

    index_root = repo / ".code-indexer" / "index"
    collections = [path for path in index_root.iterdir() if path.is_dir()]
    assert collections
    store = FilesystemVectorStore(index_root, project_root=repo)
    for collection in collections:
        assert resolve_chunk_layout(collection) == ChunkLayout.SHARDED_JSON
        assert store.count_points(collection.name) > 0

    # An operator explicitly asks for a full CHUNKS_DB rebuild via --clear.
    # This must recover the damaged/legacy collection to CHUNKS_DB, not
    # leave it stuck -- matching the acceptance criterion that clear=true
    # is a genuine "from scratch" rebuild.
    recovery = subprocess.run(
        ["cidx", "index", "--clear", "--new-collection-layout=chunks_db"],
        cwd=repo,
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert recovery.returncode == 0, recovery.stderr or recovery.stdout

    rebuilt = [path for path in index_root.iterdir() if path.is_dir()]
    assert rebuilt
    rebuilt_store = FilesystemVectorStore(index_root, project_root=repo)
    for collection in rebuilt:
        assert resolve_chunk_layout(collection) == ChunkLayout.CHUNKS_DB
        assert rebuilt_store.count_points(collection.name) > 0

    leftover_json_shards = list(index_root.rglob("vector_*.json"))
    assert leftover_json_shards == []
