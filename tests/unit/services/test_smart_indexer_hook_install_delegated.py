"""SmartIndexer installs the post-checkout hook only into a local repository.

Through the git seam (``git_backend`` set) the repository's ``.git`` is not on
this machine, so there is no hooks directory to write into: the
GitHookManager is never consulted. Without a git backend it is installed as
before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer


class _StopAfterHook(Exception):
    """Raised by the first step after the hook install, ending the pass."""


def _indexer(
    tmp_path: Path, git_backend: Optional[Any]
) -> Tuple[SmartIndexer, MagicMock]:
    root = tmp_path / "repo"
    root.mkdir()
    indexer = SmartIndexer(
        config=Config(codebase_dir=root),
        embedding_provider=MagicMock(),
        vector_store_client=MagicMock(),
        metadata_path=tmp_path / "metadata.json",
        git_backend=git_backend,
    )
    hooks = MagicMock()
    topology = MagicMock()
    topology.invalidate_cache.side_effect = _StopAfterHook
    indexer.git_hook_manager = hooks
    indexer.git_topology_service = topology
    setattr(indexer, "get_git_status", MagicMock(return_value={"git_available": True}))
    return indexer, hooks


def _run_until_after_hook(indexer: SmartIndexer) -> None:
    with pytest.raises(_StopAfterHook):
        indexer._smart_index(force_full=False, reconcile_with_database=False)


def test_delegated_git_never_touches_the_hook_manager(tmp_path: Path) -> None:
    indexer, hooks = _indexer(tmp_path, git_backend=MagicMock())
    _run_until_after_hook(indexer)
    assert hooks.mock_calls == []


def test_local_git_installs_the_hook(tmp_path: Path) -> None:
    indexer, hooks = _indexer(tmp_path, git_backend=None)
    _run_until_after_hook(indexer)
    hooks.ensure_hook_installed.assert_called_once_with()


def test_local_hook_failure_is_still_non_fatal(tmp_path: Path) -> None:
    indexer, hooks = _indexer(tmp_path, git_backend=None)
    hooks.ensure_hook_installed.side_effect = OSError("ro")
    _run_until_after_hook(indexer)
