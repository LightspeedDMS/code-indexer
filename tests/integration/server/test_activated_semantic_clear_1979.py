"""Bug #1979: activated semantic clear rebuilds an unchanged repository."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout


def test_semantic_clear_rebuilds_unchanged_repository(
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
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server-data"))
    child_env = os.environ.copy()

    initialized = subprocess.run(
        ["cidx", "init"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert initialized.returncode == 0, initialized.stderr or initialized.stdout
    first_index = subprocess.run(
        ["cidx", "index", "--new-collection-layout=chunks_db"],
        cwd=repo,
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert first_index.returncode == 0, first_index.stderr or first_index.stdout

    index_root = repo / ".code-indexer" / "index"
    collections = [path for path in index_root.iterdir() if path.is_dir()]
    assert collections
    store = FilesystemVectorStore(index_root, project_root=repo)
    for collection in collections:
        assert resolve_chunk_layout(collection) == ChunkLayout.CHUNKS_DB
        assert store.count_points(collection.name) > 0

    # Keep the repository and progress files unchanged between the two runs.
    # Bug #1979 only reproduces when SmartIndexer's Track-2 mtime scan
    # (find_modified_files(resume_timestamp), smart_indexer.py ~1546)
    # genuinely finds zero changed files. resume_timestamp is
    # last_index_timestamp minus a 60s safety buffer (progressive_metadata.py
    # get_resume_timestamp), so in a fast test the source file's real mtime
    # (written seconds before the first index) falls INSIDE that 60s buffer
    # and is wrongly re-detected as "modified" -- masking the bug. Backdate
    # the file's mtime well past the buffer so the second run is a genuine
    # zero-changes case, matching the real-world scenario (an unchanged repo
    # reindexed hours/days later).
    backdated = os.stat(repo / "app.py").st_mtime - 3600
    os.utime(repo / "app.py", (backdated, backdated))

    manager = ActivatedRepoIndexManager.__new__(ActivatedRepoIndexManager)
    manager.logger = logging.getLogger(__name__)
    monkeypatch.setattr(manager, "_seed_telemetry", lambda _repo: None)
    monkeypatch.setattr(manager, "_drain_telemetry", lambda _repo: None)
    result = manager._execute_semantic_indexing(str(repo), clear=True)
    assert result["success"], result

    rebuilt = [path for path in index_root.iterdir() if path.is_dir()]
    assert rebuilt
    rebuilt_store = FilesystemVectorStore(index_root, project_root=repo)
    for collection in rebuilt:
        assert resolve_chunk_layout(collection) == ChunkLayout.CHUNKS_DB
        assert rebuilt_store.count_points(collection.name) > 0


def test_semantic_clear_allows_empty_multimodal_collection(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "example-repo"
    repo.mkdir()
    (repo / "app.py").write_text("def example_service() -> str:\n    return 'ready'\n")

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
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server-data"))
    child_env = os.environ.copy()

    initialized = subprocess.run(
        ["cidx", "init"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert initialized.returncode == 0, initialized.stderr or initialized.stdout
    first_index = subprocess.run(
        ["cidx", "index", "--new-collection-layout=chunks_db"],
        cwd=repo,
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert first_index.returncode == 0, first_index.stderr or first_index.stdout

    index_root = repo / ".code-indexer" / "index"
    store = FilesystemVectorStore(index_root, project_root=repo)
    assert store.count_points("voyage-code-3") > 0
    # A prior empty multimodal collection is optional in a no-image repo.
    store.create_collection("voyage-multimodal-3", 1024)
    assert store.count_points("voyage-multimodal-3") == 0

    manager = ActivatedRepoIndexManager.__new__(ActivatedRepoIndexManager)
    manager.logger = logging.getLogger(__name__)
    monkeypatch.setattr(manager, "_seed_telemetry", lambda _repo: None)
    monkeypatch.setattr(manager, "_drain_telemetry", lambda _repo: None)
    result = manager._execute_semantic_indexing(str(repo), clear=True)

    assert result["success"], result
    rebuilt_store = FilesystemVectorStore(index_root, project_root=repo)
    assert rebuilt_store.count_points("voyage-code-3") > 0
    assert rebuilt_store.count_points("voyage-multimodal-3") == 0
