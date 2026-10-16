"""Indexing never reads a file whose RESOLVED location is inside the
repository's own ``.git`` -- in local CLI and server context alike.

Local CLI indexing still follows symlinks that point outside the
repository (test_symlink_containment_context.py); only targets inside the
repository's own ``.git`` are excluded in every mode.

Real files and real symlinks throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.config import Config
from code_indexer.indexing.file_finder import FileFinder
from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
from code_indexer.indexing.processor import DocumentProcessor
from code_indexer.server.utils.server_managed_provider_settings import (
    enforce_server_managed_provider_settings,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git" / "info").mkdir(parents=True)
    (repo / ".git" / "config").write_text('[remote "origin"]\n\turl = example\n')
    (repo / ".git" / "info" / "exclude").write_text("main.py\n")
    (repo / "main.py").write_text("def main():\n    return 1\n")
    return repo


def _config(repo: Path, server: bool) -> Config:
    config = Config(codebase_dir=repo)
    if server:
        enforce_server_managed_provider_settings(config)
    return config


@pytest.mark.parametrize("server", [False, True])
def test_gitignore_resolving_into_git_directory_is_not_read(
    tmp_path: Path, server: bool
) -> None:
    repo = _repo(tmp_path)
    (repo / ".gitignore").symlink_to(".git/info/exclude")

    found = set(FileFinder(_config(repo, server)).find_files())

    assert repo / "main.py" in found, found


def _repo_with_links(tmp_path: Path) -> Path:
    """The repo plus ``link.py -> .git/config`` and ``outside.py`` pointing
    to a file outside the repository."""
    repo = _repo(tmp_path)
    (repo / "link.py").symlink_to(".git/config")
    (tmp_path / "shared.py").write_text("def shared():\n    return 2\n")
    (repo / "outside.py").symlink_to(tmp_path / "shared.py")
    return repo


@pytest.mark.parametrize("server", [False, True])
def test_find_files_excludes_git_backed_symlink_in_every_mode(
    tmp_path: Path, server: bool
) -> None:
    repo = _repo_with_links(tmp_path)

    found = set(FileFinder(_config(repo, server)).find_files())

    assert repo / "main.py" in found, found
    assert repo / "link.py" not in found, found
    # Local CLI keeps following symlinks out of the repository.
    assert (repo / "outside.py" in found) is (not server), found


@pytest.mark.parametrize("server", [False, True])
def test_chunker_refuses_git_backed_symlink_in_every_mode(
    tmp_path: Path, server: bool
) -> None:
    repo = _repo_with_links(tmp_path)
    chunker = FixedSizeChunker(_config(repo, server))

    with pytest.raises(ValueError):
        chunker.chunk_file(repo / "link.py", repo_root=repo)
    assert chunker.chunk_file(repo / "main.py", repo_root=repo)


@pytest.mark.parametrize("server", [False, True])
def test_candidate_filter_drops_git_backed_symlink_in_every_mode(
    tmp_path: Path, server: bool
) -> None:
    """Git-diff, watch and branch-change candidate lists."""
    repo = _repo_with_links(tmp_path)
    processor = DocumentProcessor(
        _config(repo, server),
        embedding_provider=None,  # type: ignore[arg-type]
        vector_store_client=None,
    )

    kept = processor._filter_paths_within_codebase_root(
        [repo / "main.py", repo / "link.py", repo / "outside.py"]
    )

    expected = [repo / "main.py"] if server else [repo / "main.py", repo / "outside.py"]
    assert kept == expected
