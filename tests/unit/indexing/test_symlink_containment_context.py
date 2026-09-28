"""Out-of-root symlink containment applies to server context only.

Server indexing keeps every indexed or read file inside the resolved
repository root: a symlink whose target resolves outside the repository
root is not indexed. A local CLI user indexing their own checkout
legitimately links to directories outside it; local CLI indexing therefore
follows symlinks exactly as it always did.

The server-context signal is the existing server seam:
``enforce_server_managed_provider_settings`` (stamped on every
server-spawned ``cidx index`` through ``--server-managed-provider-settings``
and applied at every server repo-config load point) marks the loaded
``Config`` as confined to the codebase root. A plain loaded ``Config`` is
local CLI context.

All tests use real files, real symlinks and the real components.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from code_indexer.config import Config, ConfigManager
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.indexing.file_finder import FileFinder
from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
from code_indexer.indexing.processor import DocumentProcessor
from code_indexer.server.utils.server_managed_provider_settings import (
    enforce_server_managed_provider_settings,
)
from code_indexer.services.smart_indexer import SmartIndexer

_OUTSIDE_CONTENT = "def shared_helper():\n    return 'outside the repository'\n"


def _repo_with_out_of_root_symlink(tmp_path: Path) -> Tuple[Path, Path]:
    """A repo holding one regular file and one symlink whose target lies in
    a sibling directory outside the repo. Returns (repo, symlink)."""
    outside = tmp_path / "shared-lib"
    outside.mkdir()
    (outside / "helper.py").write_text(_OUTSIDE_CONTENT)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("def main():\n    return 1\n")
    link = repo / "helper.py"
    link.symlink_to(outside / "helper.py")
    return repo, link


def _local_config(repo: Path) -> Config:
    return Config(codebase_dir=repo)


def _server_config(repo: Path) -> Config:
    config = Config(codebase_dir=repo)
    enforce_server_managed_provider_settings(config)
    return config


class TestFileFinderContext:
    def test_local_cli_file_finder_includes_out_of_root_symlink(
        self, tmp_path: Path
    ) -> None:
        repo, link = _repo_with_out_of_root_symlink(tmp_path)

        found = set(FileFinder(_local_config(repo)).find_files())

        assert link in found, (
            "Local CLI indexing must follow an in-repo symlink to a file "
            f"outside the repository, as it always did. Found: {sorted(found)}"
        )
        assert repo / "main.py" in found

    def test_server_file_finder_excludes_out_of_root_symlink(
        self, tmp_path: Path
    ) -> None:
        repo, link = _repo_with_out_of_root_symlink(tmp_path)

        found = set(FileFinder(_server_config(repo)).find_files())

        assert link not in found
        assert repo / "main.py" in found


class TestChunkerReadTimeContext:
    def test_local_cli_chunker_reads_out_of_root_symlink(self, tmp_path: Path) -> None:
        repo, link = _repo_with_out_of_root_symlink(tmp_path)

        chunks = FixedSizeChunker(_local_config(repo)).chunk_file(link, repo_root=repo)

        assert chunks, "local CLI chunking must read the symlinked file"
        assert "outside the repository" in chunks[0]["text"]

    def test_server_chunker_refuses_out_of_root_symlink(self, tmp_path: Path) -> None:
        repo, link = _repo_with_out_of_root_symlink(tmp_path)

        with pytest.raises(ValueError, match="inside the codebase root"):
            FixedSizeChunker(_server_config(repo)).chunk_file(link, repo_root=repo)


def _processor(config: Config) -> DocumentProcessor:
    return DocumentProcessor(config, embedding_provider=None, vector_store_client=None)  # type: ignore[arg-type]


class TestDiffListContext:
    """Incremental (git-diff), watch and branch-change candidate lists all
    pass through the processor's shared containment filter."""

    def test_local_cli_diff_list_keeps_out_of_root_symlink(
        self, tmp_path: Path
    ) -> None:
        repo, link = _repo_with_out_of_root_symlink(tmp_path)

        kept = _processor(_local_config(repo))._filter_paths_within_codebase_root(
            [repo / "main.py", link]
        )

        assert kept == [repo / "main.py", link]

    def test_server_diff_list_drops_out_of_root_symlink(self, tmp_path: Path) -> None:
        repo, link = _repo_with_out_of_root_symlink(tmp_path)

        kept = _processor(_server_config(repo))._filter_paths_within_codebase_root(
            [repo / "main.py", link]
        )

        assert kept == [repo / "main.py"]


class TestMarkerNotRepositoryControlled:
    """The server-context marker is runtime-only: a repository-authored
    config.json can neither set nor clear it."""

    @pytest.mark.parametrize(
        "config_fields",
        [
            {"_confined_to_codebase_root": False},
            {"confined_to_codebase_root": False},
        ],
    )
    def test_config_json_cannot_clear_server_marker(
        self, tmp_path: Path, config_fields: Dict[str, Any]
    ) -> None:
        repo = tmp_path / "repo"
        (repo / ".code-indexer").mkdir(parents=True)
        (repo / ".code-indexer" / "config.json").write_text(
            json.dumps({"codebase_dir": str(repo), **config_fields})
        )
        config = ConfigManager(repo / ".code-indexer" / "config.json").load()
        enforce_server_managed_provider_settings(config)

        assert config.confined_to_codebase_root

    def test_config_json_cannot_set_marker(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / ".code-indexer").mkdir(parents=True)
        (repo / ".code-indexer" / "config.json").write_text(
            json.dumps({"codebase_dir": str(repo), "_confined_to_codebase_root": True})
        )
        config = ConfigManager(repo / ".code-indexer" / "config.json").load()

        assert not config.confined_to_codebase_root
        assert "_confined_to_codebase_root" not in config.model_dump()


class _LocalHashEmbeddingProvider(EmbeddingProvider):
    """Real, deterministic, network-free provider for full indexing runs."""

    _DIM = 16

    def _vector(self, text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i] / 255.0 for i in range(self._DIM)]

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ) -> List[float]:
        return self._vector(text)

    def get_embeddings_batch(
        self, texts: List[str], model: Optional[str] = None
    ) -> List[List[float]]:
        return [self._vector(t) for t in texts]

    def get_embedding_with_metadata(
        self, text: str, model: Optional[str] = None
    ) -> EmbeddingResult:
        return EmbeddingResult(embedding=self._vector(text), model="local-hash")

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], model: Optional[str] = None
    ) -> BatchEmbeddingResult:
        return BatchEmbeddingResult(
            embeddings=[self._vector(t) for t in texts], model="local-hash"
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, int]:
        return {"dimensions": self._DIM, "max_tokens": 8192}

    def get_provider_name(self) -> str:
        return "local-hash-provider"

    def get_current_model(self) -> str:
        return "local-hash"

    def supports_batch_processing(self) -> bool:
        return True

    def _get_model_token_limit(self) -> int:
        return 8192


def _full_index(config: Config, repo: Path) -> Tuple[int, List[str]]:
    """Run a real full index; return (files processed, stored work list)."""
    provider = _LocalHashEmbeddingProvider()
    store = FilesystemVectorStore(base_path=repo / ".code-indexer" / "index")
    store.ensure_provider_aware_collection(config, provider)
    metadata_path = repo / ".code-indexer" / "metadata-local-hash-provider.json"
    stats = SmartIndexer(config, provider, store, metadata_path).smart_index(
        force_full=True
    )
    stored = json.loads(metadata_path.read_text())["files_to_index"]
    return stats.files_processed, [Path(p).name for p in stored]


class TestFullIndexContext:
    def test_local_cli_full_index_includes_out_of_root_symlink(
        self, tmp_path: Path
    ) -> None:
        repo, _ = _repo_with_out_of_root_symlink(tmp_path)

        processed, stored = _full_index(_local_config(repo), repo)

        assert processed == 2
        assert sorted(stored) == ["helper.py", "main.py"]

    def test_server_full_index_excludes_out_of_root_symlink(
        self, tmp_path: Path
    ) -> None:
        repo, _ = _repo_with_out_of_root_symlink(tmp_path)

        processed, stored = _full_index(_server_config(repo), repo)

        assert processed == 1
        assert stored == ["main.py"]


def _smart_indexer(config: Config, tmp_path: Path) -> SmartIndexer:
    return SmartIndexer(
        config=config,
        embedding_provider=MagicMock(),
        vector_store_client=MagicMock(),
        metadata_path=tmp_path / "metadata.json",
    )


class TestResumeCandidateContext:
    """A resumed run re-checks each stored entry the way a fresh walk in the
    same context would include it."""

    def test_local_cli_resume_keeps_out_of_root_symlink(self, tmp_path: Path) -> None:
        repo, link = _repo_with_out_of_root_symlink(tmp_path)
        indexer = _smart_indexer(_local_config(repo), tmp_path)

        assert indexer._resume_candidate_is_safe(link, repo.resolve())

    def test_server_resume_drops_out_of_root_symlink(self, tmp_path: Path) -> None:
        repo, link = _repo_with_out_of_root_symlink(tmp_path)
        indexer = _smart_indexer(_server_config(repo), tmp_path)

        assert not indexer._resume_candidate_is_safe(link, repo.resolve())

    @pytest.mark.parametrize("server", [False, True])
    def test_dot_dot_traversal_entry_is_rejected_in_both_contexts(
        self, tmp_path: Path, server: bool
    ) -> None:
        repo, _ = _repo_with_out_of_root_symlink(tmp_path)
        config = _server_config(repo) if server else _local_config(repo)
        indexer = _smart_indexer(config, tmp_path)
        traversal = repo / ".." / "shared-lib" / "helper.py"

        assert not indexer._resume_candidate_is_safe(traversal, repo.resolve())

    @pytest.mark.parametrize("server", [False, True])
    def test_entry_under_symlinked_directory_is_rejected_in_both_contexts(
        self, tmp_path: Path, server: bool
    ) -> None:
        """A fresh walk never descends into a symlinked directory, so a
        resume entry below one is never something the walk would index."""
        repo, _ = _repo_with_out_of_root_symlink(tmp_path)
        (repo / "linked-dir").symlink_to(tmp_path / "shared-lib")
        config = _server_config(repo) if server else _local_config(repo)
        indexer = _smart_indexer(config, tmp_path)

        assert not indexer._resume_candidate_is_safe(
            repo / "linked-dir" / "helper.py", repo.resolve()
        )
