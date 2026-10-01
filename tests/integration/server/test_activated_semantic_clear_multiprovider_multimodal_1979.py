"""Bug #1979 step 5: after a zero-changes clear=true, EVERY configured
provider's collection (voyage-ai and cohere) and EVERY multimodal
collection (when the repo has image content) must come back populated and
CHUNKS_DB -- not just the single default-provider text collection the
step-1 test covers.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout

# Minimal valid 1x1 red-pixel PNG (neutral, no external dependency).
_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+A8AAQUBAScY"
    "42YAAAAASUVORK5CYII="
)

_SITECUSTOMIZE = """
from code_indexer.services.voyage_ai import VoyageAIClient
from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
from code_indexer.services.voyage_multimodal import VoyageMultimodalClient
from code_indexer.services.cohere_multimodal import CohereMultimodalClient


def _fake_text_batch(self, texts, model=None, *, embedding_purpose="document", retry=True):
    return [[1.0] + [0.0] * (self.get_model_info()["dimensions"] - 1) for _ in texts]


def _fake_multimodal_batch(self, items, input_type=None):
    dims = self.get_model_info()["dimensions"]
    return [[1.0] + [0.0] * (dims - 1) for _ in items]


def _fake_multimodal_single(self, text, image_paths, input_type=None):
    dims = self.get_model_info()["dimensions"]
    return [1.0] + [0.0] * (dims - 1)


def _fake_health_check(self, *, test_api=False):
    return True


VoyageAIClient.get_embeddings_batch = _fake_text_batch
CohereEmbeddingProvider.get_embeddings_batch = _fake_text_batch
CohereEmbeddingProvider.health_check = _fake_health_check
VoyageMultimodalClient.get_embeddings_batch = _fake_text_batch
VoyageMultimodalClient.get_multimodal_embeddings_batch = _fake_multimodal_batch
# FileChunkingManager._process_multimodal_file (file_chunking_manager.py
# ~748) calls the SINGULAR get_multimodal_embedding for actual per-image
# indexing -- the batch method above is query-path only.
VoyageMultimodalClient.get_multimodal_embedding = _fake_multimodal_single
CohereMultimodalClient.get_embeddings_batch = _fake_text_batch
CohereMultimodalClient.get_multimodal_embeddings_batch = _fake_multimodal_batch
CohereMultimodalClient.get_multimodal_embedding = _fake_multimodal_single
"""


def _populate_repo(repo: Path) -> None:
    repo.mkdir()
    (repo / "app.py").write_text(
        "def greet(name: str) -> str:\n"
        "    return f'Hello, {name}'\n"
        "\n"
        "def farewell(name: str) -> str:\n"
        "    return f'Goodbye, {name}'\n"
    )
    images_dir = repo / "images"
    images_dir.mkdir()
    (images_dir / "pixel.png").write_bytes(_PIXEL_PNG)
    # Multimodal indexing discovers images via references inside markdown
    # (or HTML) documents, not by scanning an images/ directory directly --
    # confirmed against src/code_indexer/indexing/image_extractor.py, which
    # parses markdown/HTML image links.
    docs_dir = repo / "docs"
    docs_dir.mkdir()
    (docs_dir / "README.md").write_text(
        "# Example docs\n\n"
        "See the diagram below.\n\n"
        "![pixel diagram](../images/pixel.png)\n"
    )


def _dual_provider_env(tmp_path: Path, monkeypatch) -> dict:
    child_bootstrap = tmp_path / "child-bootstrap"
    child_bootstrap.mkdir()
    (child_bootstrap / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    project_src = Path(__file__).resolve().parents[3] / "src"
    child_pythonpath = os.pathsep.join((str(child_bootstrap), str(project_src)))
    monkeypatch.setenv("PYTHONPATH", child_pythonpath)
    monkeypatch.setenv("VOYAGE_API_KEY", "example-key")
    monkeypatch.setenv("CO_API_KEY", "example-key")
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server-data"))
    return os.environ.copy()


def _enable_dual_provider(repo: Path) -> None:
    import json

    config_path = repo / ".code-indexer" / "config.json"
    config = json.loads(config_path.read_text())
    config["embedding_providers"] = ["voyage-ai", "cohere"]
    config_path.write_text(json.dumps(config))


def test_multiprovider_multimodal_clear_repopulates_every_collection(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "example-repo"
    _populate_repo(repo)
    child_env = _dual_provider_env(tmp_path, monkeypatch)

    initialized = subprocess.run(
        ["cidx", "init"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert initialized.returncode == 0, initialized.stderr or initialized.stdout
    _enable_dual_provider(repo)

    first_index = subprocess.run(
        ["cidx", "index", "--new-collection-layout=chunks_db"],
        cwd=repo,
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert first_index.returncode == 0, first_index.stderr or first_index.stdout

    index_root = repo / ".code-indexer" / "index"
    initial_collections = {
        path.name: path for path in index_root.iterdir() if path.is_dir()
    }
    # Both providers' text collections AND both multimodal collections.
    assert "voyage-code-3" in initial_collections
    assert "embed-v4.0" in initial_collections
    assert "voyage-multimodal-3" in initial_collections
    assert "embed-v4.0-multimodal" in initial_collections

    store = FilesystemVectorStore(index_root, project_root=repo)
    for name, path in initial_collections.items():
        assert resolve_chunk_layout(path) == ChunkLayout.CHUNKS_DB, name
        assert store.count_points(name) > 0, name

    # Zero changes since the last index (same mtime-backdating technique as
    # the step-1 test -- see its comment for why this is necessary).
    backdated = os.stat(repo / "app.py").st_mtime - 3600
    os.utime(repo / "app.py", (backdated, backdated))
    backdated_img = os.stat(repo / "images" / "pixel.png").st_mtime - 3600
    os.utime(repo / "images" / "pixel.png", (backdated_img, backdated_img))
    backdated_doc = os.stat(repo / "docs" / "README.md").st_mtime - 3600
    os.utime(repo / "docs" / "README.md", (backdated_doc, backdated_doc))

    manager = ActivatedRepoIndexManager.__new__(ActivatedRepoIndexManager)
    manager.logger = logging.getLogger(__name__)
    monkeypatch.setattr(manager, "_seed_telemetry", lambda _repo: None)
    monkeypatch.setattr(manager, "_drain_telemetry", lambda _repo: None)
    result = manager._execute_semantic_indexing(str(repo), clear=True)
    assert result["success"], result

    rebuilt_collections = {
        path.name: path for path in index_root.iterdir() if path.is_dir()
    }
    assert "voyage-code-3" in rebuilt_collections
    assert "embed-v4.0" in rebuilt_collections
    assert "voyage-multimodal-3" in rebuilt_collections
    assert "embed-v4.0-multimodal" in rebuilt_collections

    rebuilt_store = FilesystemVectorStore(index_root, project_root=repo)
    for name, path in rebuilt_collections.items():
        assert resolve_chunk_layout(path) == ChunkLayout.CHUNKS_DB, name
        assert rebuilt_store.count_points(name) > 0, name

    leftover_json_shards = list(index_root.rglob("vector_*.json"))
    assert leftover_json_shards == []


@pytest.mark.parametrize("front_door", ["cli", "server"])
def test_clear_removes_deleted_image_from_each_multimodal_collection(
    tmp_path: Path, monkeypatch, front_door: str
) -> None:
    repo = tmp_path / "example-repo"
    _populate_repo(repo)
    child_env = _dual_provider_env(tmp_path, monkeypatch)

    initialized = subprocess.run(
        ["cidx", "init"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert initialized.returncode == 0, initialized.stderr or initialized.stdout
    _enable_dual_provider(repo)

    # The first run writes the CLI's legacy layout; the clear explicitly
    # requests CHUNKS_DB and must convert even an emptied multimodal index.
    first_index = subprocess.run(
        ["cidx", "index"], cwd=repo, env=child_env, text=True, capture_output=True
    )
    assert first_index.returncode == 0, first_index.stderr or first_index.stdout

    index_root = repo / ".code-indexer" / "index"
    store = FilesystemVectorStore(index_root, project_root=repo)
    text_names = ("voyage-code-3", "embed-v4.0")
    multimodal_names = ("voyage-multimodal-3", "embed-v4.0-multimodal")
    for name in (*text_names, *multimodal_names):
        assert store.count_points(name) > 0, name
        assert resolve_chunk_layout(index_root / name) == ChunkLayout.SHARDED_JSON

    (repo / "images" / "pixel.png").unlink()
    (repo / "docs" / "README.md").write_text(
        "# Example docs\n\nThe diagram has been removed.\n"
    )
    for name in (*text_names, *multimodal_names):
        assert store.count_points(name) > 0, name
        assert resolve_chunk_layout(index_root / name) == ChunkLayout.SHARDED_JSON

    if front_door == "cli":
        cleared = subprocess.run(
            ["cidx", "index", "--clear", "--new-collection-layout=chunks_db"],
            cwd=repo,
            env=child_env,
            text=True,
            capture_output=True,
        )
        assert cleared.returncode == 0, cleared.stderr or cleared.stdout
    else:
        manager = ActivatedRepoIndexManager.__new__(ActivatedRepoIndexManager)
        manager.logger = logging.getLogger(__name__)
        monkeypatch.setattr(manager, "_seed_telemetry", lambda _repo: None)
        monkeypatch.setattr(manager, "_drain_telemetry", lambda _repo: None)
        result = manager._execute_semantic_indexing(str(repo), clear=True)
        assert result["success"], result

    rebuilt_store = FilesystemVectorStore(index_root, project_root=repo)
    for name in text_names:
        assert rebuilt_store.count_points(name) > 0, name
        assert resolve_chunk_layout(index_root / name) == ChunkLayout.CHUNKS_DB
    for name in multimodal_names:
        assert rebuilt_store.count_points(name) == 0, name
        assert resolve_chunk_layout(index_root / name) == ChunkLayout.CHUNKS_DB
