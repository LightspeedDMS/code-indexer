"""A delegated indexing pass asks its manifest, never this pod's disk or git.

Each of these ran a local access on a pod that does not hold the repository and
took the empty answer as the truth:

* reconcile's content id stat()ed each file (non-git: cidx-meta) or ran
  ``git log`` for one missing from the HEAD map -> ``working_dir_error`` ids
  that never match the stored ``blob:`` ids;
* resume re-anchoring probed candidate paths with ``exists()``;
* FileIdentifier hashed a file the delegated manifest had no hash for by
  reading a path that is not here -> None -> a silently different point id;
* the vector store asked local git for the repository root on every upsert;
* a multimodal image path was ``resolve()``d against a disk without the repo.

Local behaviour is pinned unchanged alongside.
"""

from __future__ import annotations

import builtins
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from code_indexer.config import Config
from code_indexer.indexing.file_facts import ContentUnavailableError
from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import ContentEntry
from code_indexer.services.file_chunking_manager import FileChunkingManager
from code_indexer.services.file_identifier import FileIdentifier
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

SHA = "b" * 40


@pytest.fixture
def codebase(tmp_path: Path) -> Path:
    """What a mount-less pod holds at the golden repo's path: index state only."""
    here = tmp_path / "golden-repos" / "cidx-meta"
    (here / ".code-indexer").mkdir(parents=True)
    return here


def _manifest(root: Path, *, local: bool = False) -> RepoManifest:
    entries = {
        "a.md": ContentEntry(path="a.md", size=3, blob_sha=SHA, is_symlink=False),
        "docs/b.md": ContentEntry(
            path="docs/b.md", size=3, blob_sha=SHA, is_symlink=False
        ),
        "nohash.md": ContentEntry(
            path="nohash.md", size=3, blob_sha=None, is_symlink=False
        ),
    }
    return RepoManifest(root, entries, backend_is_local=local)


class _NoLocalAccess:
    """Fails the test on any stat/exists/open under ``root``, or any git spawn."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
        prefix = os.path.abspath(root)

        def _under(path: Any) -> bool:
            try:
                return bool(os.path.abspath(os.fspath(path)).startswith(prefix))
            except TypeError:
                return False

        def guard(owner: Any, name: str) -> None:
            original = getattr(owner, name)

            def wrapper(target: Any, *a: Any, **k: Any) -> Any:
                if _under(target):
                    raise AssertionError(f"local {name}: {target}")
                return original(target, *a, **k)

            monkeypatch.setattr(owner, name, wrapper)

        guard(Path, "stat")
        guard(Path, "exists")
        guard(builtins, "open")

        def no_git(argv: Any, *a: Any, **k: Any) -> Any:
            raise AssertionError(f"local git: {argv!r}")

        monkeypatch.setattr(subprocess, "run", no_git)


def _reconcile_indexer(codebase: Path, manifest: Any) -> Any:
    indexer: Any = SmartIndexer.__new__(SmartIndexer)
    indexer.config = SimpleNamespace(codebase_dir=codebase)
    indexer.repo_manifest = manifest
    indexer.git_topology_service = MagicMock()
    indexer.git_topology_service.is_git_available.return_value = False
    return indexer


# ── reconcile content id ────────────────────────────────────────────────────


def test_reconcile_content_id_is_the_manifest_blob_hash(
    codebase: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    indexer = _reconcile_indexer(codebase, _manifest(codebase))
    _NoLocalAccess(monkeypatch, codebase)
    assert indexer._get_effective_content_id_for_reconcile("a.md") == (
        f"a.md:blob:{SHA}"
    )


def test_reconcile_content_id_without_a_manifest_hash_fails_loudly(
    codebase: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    indexer = _reconcile_indexer(codebase, _manifest(codebase))
    _NoLocalAccess(monkeypatch, codebase)
    with pytest.raises(ContentUnavailableError):
        indexer._get_effective_content_id_for_reconcile("nohash.md")


def test_reconcile_content_id_without_a_manifest_still_stats(codebase: Path) -> None:
    (codebase / "a.md").write_text("abc")
    indexer = _reconcile_indexer(codebase, None)
    st = (codebase / "a.md").stat()
    assert indexer._get_effective_content_id_for_reconcile("a.md") == (
        f"a.md:working_dir:{int(st.st_mtime)}:{st.st_size}"
    )


# ── resume re-anchoring ─────────────────────────────────────────────────────


def test_reanchor_asks_the_manifest_which_candidate_exists(
    codebase: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaf repeated in the stored path (bug #1087): the first candidate is
    not in the repository, the second is -- only the manifest knows."""
    stored = "/mnt/old/cidx-meta/docs/cidx-meta/a.md"
    manifest = _manifest(codebase)
    _NoLocalAccess(monkeypatch, codebase)
    assert SmartIndexer._reanchor_resume_path(stored, codebase, manifest) == (
        codebase / "a.md"
    )


def test_reanchor_without_a_manifest_probes_the_disk(codebase: Path) -> None:
    stored = "/mnt/old/cidx-meta/docs/cidx-meta/a.md"
    assert SmartIndexer._reanchor_resume_path(stored, codebase) == (
        codebase / "docs" / "cidx-meta" / "a.md"
    ), "nothing exists here: the first match, exactly as before"
    (codebase / "a.md").write_text("x")
    assert SmartIndexer._reanchor_resume_path(stored, codebase) == codebase / "a.md"


# ── FileIdentifier blob hash on a manifest miss ─────────────────────────────


def test_a_delegated_manifest_miss_fails_instead_of_reading(
    codebase: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = FileIdentifier(
        codebase, Config(codebase_dir=codebase), manifest=_manifest(codebase)
    )
    identifier._cached_branch = "master"
    identifier._cached_commit_hash = "c" * 40
    _NoLocalAccess(monkeypatch, codebase)
    with pytest.raises(ContentUnavailableError):
        identifier._get_git_metadata(codebase / "nohash.md")


def test_a_local_manifest_miss_still_reads_the_file(codebase: Path) -> None:
    (codebase / "nohash.md").write_bytes(b"abc")
    identifier = FileIdentifier(
        codebase,
        Config(codebase_dir=codebase),
        manifest=_manifest(codebase, local=True),
    )
    from code_indexer.utils.blob_hash import git_blob_hash_bytes

    assert identifier._get_git_metadata(codebase / "nohash.md")["git_hash"] == (
        git_blob_hash_bytes(b"abc")
    )


def test_a_delegated_manifest_makes_filesystem_metadata_unreachable(
    codebase: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FileIdentifier._get_filesystem_metadata stats the file; a manifest makes
    every repository git-addressable, so a delegated pass never gets there."""
    identifier = FileIdentifier(codebase, Config(codebase_dir=codebase))
    identifier._project_id = "cidx-meta"
    identifier._cached_branch = "master"
    identifier._cached_commit_hash = "c" * 40
    identifier.set_manifest(_manifest(codebase))
    monkeypatch.setattr(
        identifier,
        "_get_filesystem_metadata",
        lambda p: pytest.fail("filesystem metadata under a manifest"),
    )
    metadata = identifier.get_file_metadata(codebase / "a.md", defer_content_hash=True)
    assert metadata["git_available"] and metadata["git_hash"] == SHA


# ── vector store repository root ────────────────────────────────────────────


def test_a_delegated_store_never_asks_local_git_for_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemVectorStore(tmp_path / "index", project_root=tmp_path)
    store.mark_repository_delegated()
    _NoLocalAccess(monkeypatch, tmp_path)
    assert store._get_repo_root() is None


def test_a_local_store_still_finds_the_git_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    store = FilesystemVectorStore(tmp_path / "index", project_root=repo)
    assert store._get_repo_root() == repo.resolve()


@pytest.mark.parametrize("is_local,marked", [(False, True), (True, False)])
def test_smart_indexer_marks_the_store_only_for_a_delegated_backend(
    is_local: bool, marked: bool, tmp_path: Path
) -> None:
    store = MagicMock()
    backend = SimpleNamespace(is_local=is_local)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "code_indexer.services.smart_indexer.HighThroughputProcessor.__init__",
            lambda self, *a, **k: None,
        )
        mp.setattr(
            "code_indexer.services.smart_indexer.GitTopologyService",
            MagicMock(),
        )
        mp.setattr("code_indexer.services.smart_indexer.GitHookManager", MagicMock())
        mp.setattr(
            "code_indexer.services.smart_indexer.ProgressiveMetadata", MagicMock()
        )
        mp.setattr(
            "code_indexer.services.indexing_progress_log.IndexingProgressLog",
            MagicMock(),
        )
        SmartIndexer(
            SimpleNamespace(codebase_dir=tmp_path),  # type: ignore[arg-type]
            MagicMock(),
            store,
            tmp_path / "meta.json",
            content_backend=backend,
        )
    assert store.mark_repository_delegated.called is marked


# ── multimodal image path ───────────────────────────────────────────────────


def test_an_image_path_is_lexical_under_an_image_source(
    codebase: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = FileChunkingManager.__new__(FileChunkingManager)
    manager.codebase_dir = codebase
    manager.image_source = MagicMock()
    monkeypatch.setattr(Path, "resolve", lambda *a, **k: pytest.fail("resolve()"))
    assert manager._image_path("docs/../img/x.png") == codebase / "img" / "x.png"


def test_an_image_path_is_resolved_on_a_local_pass(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "img").mkdir(parents=True)
    link = tmp_path / "link"
    os.symlink(real, link)
    manager = FileChunkingManager.__new__(FileChunkingManager)
    manager.codebase_dir = link
    manager.image_source = None
    assert manager._image_path("img/x.png") == real / "img" / "x.png"


# ── resume containment on a pod without the repository ─────────────────────


def test_resume_containment_is_lexical_where_the_repository_is_absent(
    codebase: Path,
) -> None:
    """resolve_if_within_root on a mount-less pod: the candidate does not exist
    here, so resolve() only collapses ".." -- in-tree candidates stay eligible,
    escapes are still rejected, and nothing of the repository is read."""
    indexer: Any = SmartIndexer.__new__(SmartIndexer)
    indexer.config = SimpleNamespace(
        codebase_dir=codebase, confined_to_codebase_root=True
    )
    indexer.file_finder = MagicMock()
    indexer.file_finder.is_eligible.return_value = True
    root = codebase.resolve()

    assert indexer._resume_candidate_is_safe(codebase / "src" / "a.py", root)
    assert indexer._resume_candidate_is_safe(codebase / "src" / ".." / "b.py", root)
    assert not indexer._resume_candidate_is_safe(codebase / ".." / "other.py", root)
