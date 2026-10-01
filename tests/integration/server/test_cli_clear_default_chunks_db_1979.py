"""Bug #1979 round 6: the maintainer's own acceptance-criteria comment on
the issue (`gh issue view 1979 --comments`) is explicit and unconditional:
"After a successful clear=true run, every configured collection (all
providers, plus multimodal when the repo has such content) is populated and
uses the CHUNKS_DB layout, whether or not the repo changed since its last
index." This is a deliberate, issue-scoped exception to the general
CLI/daemon SHARDED_JSON-default rule (Story #1488), specific to `--clear`.

This test proves the plain-CLI half of that requirement: a bare
`cidx index --clear`, with NO `--new-collection-layout` flag at all, must
still rebuild the collection as CHUNKS_DB -- the operator must not be
required to also pass `--new-collection-layout=chunks_db` (that combination
is already proven by the sibling
test_cli_clear_recovers_damaged_layout_1979.py; this test proves the SAME
recovery now happens by default under `--clear` alone).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout


def test_plain_clear_with_no_layout_flag_defaults_to_chunks_db(
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
    # test_cli_clear_recovers_damaged_layout_1979.py.
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
    # CLI's documented default (SHARDED_JSON) for a brand-new collection.
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

    # `--clear` alone, with NO --new-collection-layout flag: per the
    # maintainer's own acceptance criteria, this must still rebuild the
    # collection as CHUNKS_DB -- the operator must not need the extra flag.
    recovery = subprocess.run(
        ["cidx", "index", "--clear"],
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


def test_plain_incremental_index_still_defaults_to_sharded_json(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: this fix only changes the `--clear` default. A plain,
    non-clear `cidx index` on a brand-new collection must be UNCHANGED --
    still the CLI's documented SHARDED_JSON default."""
    repo = tmp_path / "example-repo"
    repo.mkdir()
    (repo / "app.py").write_text("def greet(name):\n    return name\n")

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

    first_index = subprocess.run(
        ["cidx", "index"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert first_index.returncode == 0, first_index.stderr or first_index.stdout

    index_root = repo / ".code-indexer" / "index"
    collections = [path for path in index_root.iterdir() if path.is_dir()]
    assert collections
    for collection in collections:
        assert resolve_chunk_layout(collection) == ChunkLayout.SHARDED_JSON
