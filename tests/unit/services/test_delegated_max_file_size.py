"""A refresh applies ``max_file_size`` exactly as the first index's walk does.

Measured on EKS (mount-less pod, delegated backends): agent-router's first
index excluded ``package-lock.json`` (1,192,749 bytes > max_file_size
1,048,576) -- 610 points. A refresh that pulled one commit touching that file
indexed it -- 953 points, the extra 343 all ``package-lock.json``. Discovery was
never the culprit (the walk sizes from the manifest under a delegated pass);
the git-delta incremental path selected the touched paths with a string-only
filter that never applied the size limit, locally and through the seam alike.

Pinned:

* delegated discovery == local discovery, on a tree just under, exactly at,
  and just over the limit;
* both deltas (local ``git diff`` and the git-backend seam over a manifest)
  route an over-limit touched file to ``deleted`` and keep the boundary file;
* through ``_do_incremental_index`` (what ``cidx index`` runs on a refresh),
  an over-limit file never reaches processing, under either backend.
"""

from __future__ import annotations

import subprocess
from contextlib import ExitStack
from pathlib import Path
from typing import Any, List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import Config
from code_indexer.indexing.file_finder import FileFinder, attach_delegated_manifest
from code_indexer.indexing.processor import ProcessingStats
from code_indexer.server.storage.shared.content_backend import LocalContentBackend
from code_indexer.server.storage.shared.git_backend import LocalGitBackend
from code_indexer.services.smart_indexer import SmartIndexer

LIMIT = 4096
UNDER, AT, OVER = "src/under.py", "src/at_limit.py", "lock/over.py"
SIZES = {UNDER: LIMIT - 1, AT: LIMIT, OVER: LIMIT + 1}


class _Remote(LocalContentBackend):
    """A delegated-shaped content backend: the local one, claiming non-local."""

    @property
    def is_local(self) -> bool:
        return False


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write(repo: Path, sizes: dict) -> None:
    for rel, size in sizes.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("#" * (size - 1) + "\n")


def _config(repo: Path) -> Config:
    config = Config(codebase_dir=repo, file_extensions=["py"])
    config.indexing.max_file_size = LIMIT
    return config


@pytest.fixture
def repo(tmp_path: Path) -> Tuple[Path, str, str]:
    """Commit 1: every file small. Commit 2: each grows to its SIZES value."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "master")
    _write(root, {rel: 10 for rel in SIZES})
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "small")
    old = _git(root, "rev-parse", "HEAD")
    _write(root, SIZES)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "grown")
    return root, old, _git(root, "rev-parse", "HEAD")


def _rel(repo: Path, paths: Any) -> List[str]:
    return sorted(str(Path(p).relative_to(repo)) for p in paths)


# -- discovery: the walk ------------------------------------------------------


def test_delegated_discovery_is_identical_to_local(repo: Any) -> None:
    root, _, _ = repo
    local = FileFinder(_config(root))
    delegated = FileFinder(_config(root))
    attach_delegated_manifest(delegated, _Remote(), root)

    assert _rel(root, delegated.find_files()) == _rel(root, local.find_files())
    assert _rel(root, local.find_files()) == sorted([UNDER, AT])
    for rel in SIZES:
        assert delegated.file_size(root / rel) == local.file_size(root / rel)
        assert (
            delegated.exceeds_max_file_size(root / rel)
            is local.exceeds_max_file_size(root / rel)
            is (rel == OVER)
        )


def test_an_unknown_size_is_not_over_the_limit_but_not_eligible(tmp_path: Path) -> None:
    finder = FileFinder(_config(tmp_path))
    missing = tmp_path / "gone.py"
    assert finder.file_size(missing) is None
    assert finder.exceeds_max_file_size(missing) is False
    assert finder.is_eligible(missing) is False


# -- the git delta a refresh takes ---------------------------------------------


def _indexer(root: Path, delegated: bool) -> SmartIndexer:
    vector_store = MagicMock()
    vector_store.resolve_collection_name.return_value = "c"
    vector_store.count_points.return_value = 0
    return SmartIndexer(
        config=_config(root),
        embedding_provider=MagicMock(),
        vector_store_client=vector_store,
        metadata_path=root.parent / "metadata.json",
        content_backend=_Remote() if delegated else None,
        git_backend=LocalGitBackend() if delegated else None,
    )


@pytest.mark.parametrize("delegated", [False, True], ids=["local", "delegated"])
def test_the_delta_routes_an_over_limit_file_to_deleted(
    repo: Any, delegated: bool
) -> None:
    root, old, new = repo
    indexer = _indexer(root, delegated)
    indexer.begin_manifest_pass()
    try:
        assert (indexer.repo_manifest is not None) is delegated
        if delegated:
            delta = indexer._git_deltas_via_seam(old, new)
        else:
            delta = indexer._get_git_deltas_since_commit(old, new)
    finally:
        indexer.end_manifest_pass()

    assert delta is not None
    assert sorted(delta.added + delta.modified) == sorted([UNDER, AT])
    assert delta.deleted == [OVER]


def _run_incremental(indexer: SmartIndexer, old: str, new: str) -> Tuple[list, list]:
    processed: List[Any] = []
    deleted: List[Any] = []
    meta = indexer.progressive_metadata
    meta.metadata["status"] = "completed"
    with ExitStack() as stack:
        for name, value in [
            ("can_resume_interrupted_operation", False),
            ("get_resume_timestamp", 1.0),
            ("get_last_indexed_commit", old),
        ]:
            stack.enter_context(patch.object(meta, name, return_value=value))
        for name in [
            "start_indexing",
            "set_files_to_index",
            "update_progress",
            "update_commit_watermark",
            "complete_indexing",
        ]:
            stack.enter_context(patch.object(meta, name))
        stack.enter_context(
            patch.object(indexer.progress_log, "start_session", return_value="s")
        )
        stack.enter_context(patch.object(indexer.progress_log, "complete_session"))
        stack.enter_context(
            patch.object(
                indexer.git_topology_service,
                "get_current_branch",
                return_value="master",
            )
        )
        stack.enter_context(
            patch.object(
                indexer,
                "_delete_files_from_backend",
                side_effect=lambda files, *a, **k: deleted.extend(files) or 0,
            )
        )
        stack.enter_context(
            patch.object(
                indexer,
                "process_files_high_throughput",
                side_effect=lambda files, *a, **k: (
                    processed.extend(files) or ProcessingStats()
                ),
            )
        )
        indexer.begin_manifest_pass()
        try:
            indexer._do_incremental_index(
                batch_size=10,
                progress_callback=None,
                git_status={
                    "git_available": True,
                    "current_branch": "master",
                    "current_commit": new,
                },
                provider_name="voyage-ai",
                model_name="voyage-code-3",
                safety_buffer_seconds=0,
                quiet=True,
            )
        finally:
            indexer.end_manifest_pass()
    return processed, deleted


@pytest.mark.parametrize("delegated", [False, True], ids=["local", "delegated"])
def test_a_refresh_never_processes_an_over_limit_file(
    repo: Any, delegated: bool
) -> None:
    """``_do_incremental_index`` -- the pass ``cidx index`` runs on refresh.

    Track 2 (mtime) is the walk, so every file counts as modified there;
    the over-limit one must still not be processed.
    """
    root, old, new = repo
    processed, deleted = _run_incremental(_indexer(root, delegated), old, new)

    assert _rel(root, processed) == sorted([UNDER, AT])
    assert deleted == [OVER]


def test_under_a_delegated_pass_the_manifest_size_decides(repo: Any) -> None:
    """The pod holds no repository: the disk must never answer."""
    root, _, _ = repo
    delegated = FileFinder(_config(root))
    attach_delegated_manifest(delegated, _Remote(), root)
    (root / OVER).write_text("x\n")  # the disk now disagrees with the manifest
    (root / UNDER).unlink()

    assert delegated.exceeds_max_file_size(root / OVER) is True
    assert _rel(root, delegated.find_files()) == sorted([UNDER, AT])


def test_a_rename_onto_an_over_limit_path_is_not_indexed(repo: Any) -> None:
    root, _, before = repo
    _git(root, "mv", OVER, "lock/renamed.py")
    _git(root, "commit", "-qm", "rename")
    after = _git(root, "rev-parse", "HEAD")

    delta = _indexer(root, delegated=False)._get_git_deltas_since_commit(before, after)

    assert delta.added == []
    assert sorted(delta.deleted) == sorted([OVER, "lock/renamed.py"])
