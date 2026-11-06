"""A golden-repo ADD publishes its index under a delegated IndexStore.

The add builds the index on the pod that ran it; the alias points at the clone
path until the first refresh. Without a publish here, every other pod resolves
that alias to a path it does not hold. Pinned with the REAL GoldenRepoManager,
SQLite registry, GlobalActivator and AliasManager (the background job is run
synchronously; clone and post-clone indexing are stubbed):

* delegated: the alias document carries the published version, and a pod
  that holds nothing checks exactly that index out from it;
* a failed publish fails the add and rolls the registration back;
* local: the alias document is today's -- no index fields, nothing published.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.repositories.golden_repo_manager import (
    GitOperationError,
    GoldenRepoManager,
)
from code_indexer.server.services import index_materializer as im
from code_indexer.server.startup import index_store_wiring
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    INDEX_STORE_ALIAS_FIELD,
    INDEX_VERSION_FIELD,
    DelegatedIndexStore,
    LocalIndexStore,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

ALIAS = "good-repo"


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[GoldenRepoManager]:
    mgr = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    jobs = MagicMock(spec=BackgroundJobManager)
    jobs.submit_job.return_value = "job"
    mgr.background_job_manager = jobs
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(mgr.db_path).initialize_database()
    yield mgr


def _add(manager: GoldenRepoManager) -> Any:
    clone_path = os.path.join(manager.golden_repos_dir, ALIAS)

    def clone_and_index(*args: Any, **kwargs: Any) -> str:
        index = Path(clone_path) / INDEX_DIRNAME / "index"
        index.mkdir(parents=True)
        (index / "hnsw.bin").write_bytes(b"added")
        return clone_path

    with (
        patch.object(manager, "_validate_git_repository", return_value=True),
        patch.object(manager, "_clone_repository", side_effect=clone_and_index),
        patch.object(manager, "_execute_post_clone_workflow", return_value=None),
    ):
        manager.add_golden_repo(
            repo_url=f"https://github.com/test/{ALIAS}.git",
            alias=ALIAS,
            default_branch="main",
            submitter_username="example-admin",
        )
        worker = manager.background_job_manager.submit_job.call_args[1]["func"]
        return worker()


def _document(manager: GoldenRepoManager) -> Any:
    aliases = AliasManager(os.path.join(manager.golden_repos_dir, "aliases"))
    return aliases.read_record(f"{ALIAS}-global")


def test_a_delegated_add_records_the_published_version(
    manager: GoldenRepoManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = FakeArtifacts(tmp_path / "store")
    store = DelegatedIndexStore(artifacts)
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)

    assert _add(manager)["success"] is True
    document = _document(manager)
    assert document[INDEX_STORE_ALIAS_FIELD] == ALIAS
    assert document[INDEX_VERSION_FIELD] == artifacts.current_version(ALIAS)

    other_pod = tmp_path / "pod-b" / ALIAS
    im.ensure_published_index(
        other_pod, document, store=store, fix_config=lambda p: None
    )
    assert (other_pod / INDEX_DIRNAME / "index" / "hnsw.bin").read_bytes() == b"added"


def test_a_failed_publish_fails_the_add_and_rolls_back(
    manager: GoldenRepoManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = FakeArtifacts(tmp_path / "store")
    artifacts.fail_publish = True
    store = DelegatedIndexStore(artifacts)
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)

    with pytest.raises(GitOperationError):
        _add(manager)
    assert ALIAS not in manager.golden_repos
    assert manager._sqlite_backend.get_repo(ALIAS) is None
    assert _document(manager) is None


def test_a_local_add_writes_todays_alias_document(
    manager: GoldenRepoManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        index_store_wiring, "server_index_store", lambda: LocalIndexStore()
    )
    assert _add(manager)["success"] is True
    document = _document(manager)
    assert set(document) == {"target_path", "created_at", "last_refresh", "repo_name"}
