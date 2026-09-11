"""Regression coverage for Bug #1841 orphan-clone cleanup races."""

import os

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GitOperationError,
    GoldenRepoManager,
)


@pytest.fixture
def manager(tmp_path):
    mgr = GoldenRepoManager(data_dir=str(tmp_path))
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(mgr.db_path).initialize_database()
    return mgr


def test_partial_orphan_cleanup_never_falls_through_to_clone(manager, monkeypatch):
    """A writer arriving during rmtree must block the retry clone."""
    alias = "racing-orphan"
    clone_path = os.path.join(manager.golden_repos_dir, alias)
    git_path = os.path.join(clone_path, ".git")
    os.makedirs(git_path)
    with open(os.path.join(git_path, "existing"), "w", encoding="utf-8") as stream:
        stream.write("existing")

    real_rmdir = os.rmdir
    writer_ran = False

    def rmdir_with_writer(path, *args, **kwargs):
        nonlocal writer_ran
        if path == ".git" and not writer_ran:
            writer_ran = True
            with open(
                os.path.join(git_path, "created-during-removal"), "w", encoding="utf-8"
            ) as stream:
                stream.write("writer")
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", rmdir_with_writer)

    clone_attempted = False

    def fail_if_clone_attempted(*args, **kwargs):
        nonlocal clone_attempted
        clone_attempted = True
        raise AssertionError("clone attempted after partial orphan cleanup")

    monkeypatch.setattr(manager, "_clone_repository", fail_if_clone_attempted)

    with pytest.raises(GitOperationError, match="orphan cleanup"):
        manager.execute_add_golden_repo_work(
            repo_url="https://example.invalid/repo.git",
            alias=alias,
            default_branch="main",
        )

    assert writer_ran
    assert not clone_attempted
    assert os.path.isdir(clone_path)
    assert os.path.exists(os.path.join(git_path, "created-during-removal"))
