"""Bug #1979: zero-change indexing cannot strand a new collection."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout


def test_zero_change_incremental_does_not_leave_uncommitted_collection(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "example-repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text(
        "def greet(name: str) -> str:\n"
        "    return f'Hello, {name}'\n"
        "\n"
        "def farewell(name: str) -> str:\n"
        "    return f'Goodbye, {name}'\n"
    )

    child_bootstrap = tmp_path / "child-bootstrap"
    child_bootstrap.mkdir()
    (child_bootstrap / "sitecustomize.py").write_text(
        "from code_indexer.services.voyage_ai import VoyageAIClient\n"
        "def fake_embeddings(self, texts, model=None, *, embedding_purpose='document', retry=True):\n"
        "    return [[1.0] + [0.0] * 1023 for _ in texts]\n"
        "VoyageAIClient.get_embeddings_batch = fake_embeddings\n"
    )
    project_src = Path(__file__).resolve().parents[3] / "src"
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join((str(child_bootstrap), str(project_src)))
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "example-key")
    child_env = os.environ.copy()

    initialized = subprocess.run(
        ["cidx", "init"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert initialized.returncode == 0, initialized.stderr or initialized.stdout
    indexed = subprocess.run(
        ["cidx", "index", "--new-collection-layout=chunks_db"],
        cwd=repo,
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert indexed.returncode == 0, indexed.stderr or indexed.stdout

    index_root = repo / ".code-indexer" / "index"
    assert any(index_root.iterdir())
    # Reproduce a lost index with surviving progress metadata. The old
    # incremental path recreates a collection before finding zero changes.
    shutil.rmtree(index_root)
    old_mtime = source.stat().st_mtime - 3600
    os.utime(source, (old_mtime, old_mtime))

    rerun = subprocess.run(
        ["cidx", "index", "--new-collection-layout=chunks_db"],
        cwd=repo,
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert rerun.returncode == 0, rerun.stderr or rerun.stdout
    if index_root.exists():
        for collection in index_root.iterdir():
            if collection.is_dir():
                assert resolve_chunk_layout(collection) == ChunkLayout.CHUNKS_DB
