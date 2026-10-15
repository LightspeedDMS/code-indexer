"""Golden-repo removal deletes the alias's PUBLISHED index versions.

With ``index_store_backend='delegated'`` every refresh publishes the built
index to workspace-mcp's index store as an immutable version. Those versions
live there, not in any directory removal deletes, so removal deletes them
through the server's IndexStore (``delete`` -> the plugin's ``delete_alias``):

* BEFORE the registry row goes -- a failure (ECONFLICT while a query pod holds
  a checkout lease is the realistic one) aborts the removal with the repository
  still registered, the job error saying the versions were NOT deleted and
  why, so re-running the removal completes it (``delete_alias`` is idempotent);
* the deleted count is in the job result;
* a plugin without ``delete_alias`` fails the removal, never "succeeds";
* with the local store (or no server) nothing changes.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.repositories.golden_repo_manager import (
    GitOperationError,
    GoldenRepo,
    GoldenRepoManager,
)
from code_indexer.server.services.job_tracker import DuplicateJobError
from code_indexer.server.startup import index_store_wiring
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.utils.index_store import (
    DelegatedIndexStore,
    IndexStoreDeleteUnsupportedError,
    LocalIndexStore,
    index_store_alias,
)

ALIAS = "repo1"
REMOTE_URL = "https://git.example/team/repo1.git"


class _IndexArtifactError(Exception):
    """The plugin's IndexArtifactError shape: a ``code`` and a message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class _Artifacts:
    """workspace-mcp's index store: versions per alias, delete idempotent."""

    def __init__(self, versions: Optional[Dict[str, int]] = None) -> None:
        self.versions: Dict[str, int] = dict(versions or {})
        self.deletes: List[str] = []
        self.fail: Optional[Exception] = None

    def current_version(self, alias: str) -> Optional[str]:  # pragma: no cover
        return None

    def delete_alias(self, alias: str) -> int:
        self.deletes.append(alias)
        if self.fail is not None:
            raise self.fail
        return self.versions.pop(alias, 0)


class _OldArtifacts:
    """An index-artifacts plugin older than delete support."""

    def current_version(self, alias: str) -> Optional[str]:  # pragma: no cover
        return None


def _manager(data: Path) -> GoldenRepoManager:
    mgr = GoldenRepoManager(str(data))
    bjm = MagicMock(spec=BackgroundJobManager)
    bjm.submit_job.return_value = "job-1"
    mgr.background_job_manager = bjm
    DatabaseSchema(mgr.db_path).initialize_database()
    return mgr


def _register(mgr: GoldenRepoManager, alias: str = ALIAS) -> str:
    clone_path = os.path.join(mgr.golden_repos_dir, alias)
    os.makedirs(os.path.join(clone_path, ".git"), exist_ok=True)
    repo = GoldenRepo(
        alias=alias,
        repo_url=REMOTE_URL,
        default_branch="main",
        clone_path=clone_path,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    mgr.golden_repos[alias] = repo
    mgr._sqlite_backend.add_repo(
        alias=alias,
        repo_url=REMOTE_URL,
        default_branch="main",
        clone_path=clone_path,
        created_at=repo.created_at,
    )
    return clone_path


def _remove(mgr: GoldenRepoManager, alias: str = ALIAS) -> Dict[str, Any]:
    mgr.remove_golden_repo(alias, submitter_username="admin")
    worker = mgr.background_job_manager.submit_job.call_args[1]["func"]
    return dict(worker())


def _park(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)


# ── removal under a delegated index store ──────────────────────────────


def test_remove_deletes_published_versions_and_reports_the_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _Artifacts({ALIAS: 3})
    _park(monkeypatch, DelegatedIndexStore(artifacts))
    mgr = _manager(tmp_path)
    clone = _register(mgr)

    result = _remove(mgr)

    assert artifacts.deletes == [ALIAS]
    assert result["success"] is True
    assert result["cascade_results"]["index_versions_deleted"] == 3
    assert "deleted 3 published index versions" in result["message"]
    assert mgr._sqlite_backend.get_repo(ALIAS) is None
    assert not os.path.exists(clone)


def test_remove_deletes_the_alias_refresh_publishes_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cidx aliases allow upper case; the store's do not -- same mapping."""
    artifacts = _Artifacts()
    _park(monkeypatch, DelegatedIndexStore(artifacts))
    mgr = _manager(tmp_path)
    _register(mgr, "MyRepo")

    result = _remove(mgr, "MyRepo")

    assert artifacts.deletes == [index_store_alias("MyRepo")] != ["MyRepo"]
    assert result["cascade_results"]["index_versions_deleted"] == 0


def test_alias_busy_fails_the_removal_with_the_repository_still_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _Artifacts({ALIAS: 2})
    artifacts.fail = _IndexArtifactError("ECONFLICT", "checkout lease is live")
    _park(monkeypatch, DelegatedIndexStore(artifacts))
    mgr = _manager(tmp_path)
    clone = _register(mgr)

    with pytest.raises(GitOperationError) as exc:
        _remove(mgr)

    message = str(exc.value)
    assert "index versions were NOT deleted" in message
    assert "busy" in message and "re-run the removal" in message
    assert mgr._sqlite_backend.get_repo(ALIAS) is not None
    assert os.path.exists(clone)
    assert artifacts.versions == {ALIAS: 2}


def test_rerun_after_a_failed_delete_completes_the_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _Artifacts({ALIAS: 2})
    artifacts.fail = _IndexArtifactError("ECONFLICT", "publish running")
    _park(monkeypatch, DelegatedIndexStore(artifacts))
    mgr = _manager(tmp_path)
    _register(mgr)
    with pytest.raises(GitOperationError):
        _remove(mgr)

    artifacts.fail = None
    result = _remove(mgr)

    assert result["cascade_results"]["index_versions_deleted"] == 2
    assert mgr._sqlite_backend.get_repo(ALIAS) is None


def test_rerun_after_versions_were_already_deleted_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removal that deleted the versions but then failed later re-runs
    cleanly: the alias has 0 versions left, and that is not an error."""
    artifacts = _Artifacts({ALIAS: 4})
    _park(monkeypatch, DelegatedIndexStore(artifacts))
    mgr = _manager(tmp_path)
    _register(mgr)
    with monkeypatch.context() as patch:
        patch.setattr(
            mgr._sqlite_backend,
            "remove_repo",
            MagicMock(side_effect=RuntimeError("db down")),
        )
        with pytest.raises(GitOperationError, match="db down"):
            _remove(mgr)
    assert artifacts.versions == {}

    result = _remove(mgr)

    assert artifacts.deletes == [ALIAS, ALIAS]
    assert result["cascade_results"]["index_versions_deleted"] == 0
    assert mgr._sqlite_backend.get_repo(ALIAS) is None


def test_store_unavailable_says_why_the_versions_were_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _Artifacts({ALIAS: 1})
    artifacts.fail = _IndexArtifactError("EUNAVAILABLE", "store unavailable")
    _park(monkeypatch, DelegatedIndexStore(artifacts))
    mgr = _manager(tmp_path)
    _register(mgr)

    with pytest.raises(GitOperationError, match="EUNAVAILABLE: store unavailable"):
        _remove(mgr)
    assert mgr._sqlite_backend.get_repo(ALIAS) is not None


def test_plugin_without_delete_alias_fails_the_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _park(monkeypatch, DelegatedIndexStore(_OldArtifacts()))
    mgr = _manager(tmp_path)
    _register(mgr)

    with pytest.raises(GitOperationError, match="delete_alias"):
        _remove(mgr)
    assert mgr._sqlite_backend.get_repo(ALIAS) is not None


def test_refresh_in_progress_fails_before_anything_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _Artifacts({ALIAS: 1})
    _park(monkeypatch, DelegatedIndexStore(artifacts))
    mgr = _manager(tmp_path)
    _register(mgr)
    scheduler = MagicMock()
    scheduler.check_refresh_not_in_progress.side_effect = DuplicateJobError(
        "global_repo_refresh", ALIAS, "job-9"
    )
    mgr._refresh_scheduler = scheduler

    with pytest.raises(GitOperationError, match="refresh is in progress"):
        _remove(mgr)
    assert artifacts.deletes == []
    assert mgr._sqlite_backend.get_repo(ALIAS) is not None


# ── local: unchanged ───────────────────────────────────────────────────


@pytest.mark.parametrize("store", [None, LocalIndexStore()])
def test_local_or_no_store_keeps_the_removal_result_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    _park(monkeypatch, store)
    mgr = _manager(tmp_path)
    clone = _register(mgr)

    result = _remove(mgr)

    assert result["message"] == f"Golden repository '{ALIAS}' removed successfully"
    assert "index_versions_deleted" not in result["cascade_results"]
    assert not os.path.exists(clone)


# ── IndexStore.delete ──────────────────────────────────────────────────


def test_local_store_delete_is_a_no_op() -> None:
    assert LocalIndexStore().delete("/x/golden-repos/repo1") is None


def test_delegated_store_delete_returns_the_plugin_count() -> None:
    artifacts = _Artifacts({ALIAS: 5})
    store = DelegatedIndexStore(artifacts)

    assert store.delete(Path("/x/golden-repos") / ALIAS) == 5
    assert store.delete(Path("/x/golden-repos") / ALIAS) == 0


def test_delegated_store_delete_propagates_plugin_errors() -> None:
    artifacts = _Artifacts()
    artifacts.fail = _IndexArtifactError("ETRANSPORT", "connection reset")

    with pytest.raises(_IndexArtifactError, match="ETRANSPORT"):
        DelegatedIndexStore(artifacts).delete("/x/repo1")


def test_delegated_store_delete_without_plugin_support_raises() -> None:
    with pytest.raises(IndexStoreDeleteUnsupportedError, match="_OldArtifacts"):
        DelegatedIndexStore(_OldArtifacts()).delete("/x/repo1")


# ── server_index_store ─────────────────────────────────────────────────


def test_server_index_store_is_local_outside_a_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.utils import registry_factory

    monkeypatch.setattr(registry_factory, "_running_server_app_state", lambda: None)
    store = index_store_wiring.server_index_store()
    assert store.is_local


def test_server_index_store_returns_the_parked_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.utils import registry_factory

    store = DelegatedIndexStore(_Artifacts())
    state = MagicMock(index_store=store)
    monkeypatch.setattr(registry_factory, "_running_server_app_state", lambda: state)
    assert index_store_wiring.server_index_store() is store


def test_server_index_store_rejects_a_non_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.utils import registry_factory

    state = MagicMock()
    state.index_store = object()
    monkeypatch.setattr(registry_factory, "_running_server_app_state", lambda: state)
    with pytest.raises(RuntimeError, match="not an IndexStore"):
        index_store_wiring.server_index_store()
