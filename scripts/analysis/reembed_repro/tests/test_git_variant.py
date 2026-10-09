"""Self-tests for the git variant of the synthetic repository."""

import random
import subprocess

from git_variant import (
    GIT_OPS,
    apply_op,
    choose_op,
    commit_sync,
    current_branch,
    init_git_repo,
)
from synthetic_repo import add_files, generate_repo, list_repo_files


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path):
    repo = tmp_path / "repo"
    generate_repo(repo, n_files=20, seed=1)
    (repo / ".code-indexer").mkdir()
    (repo / ".code-indexer" / "config.json").write_text("{}")
    init_git_repo(repo)
    return repo


def test_init_commits_every_file_on_main_and_ignores_the_index(tmp_path):
    repo = _repo(tmp_path)
    assert current_branch(repo) == "main"
    tracked = set(_git(repo, "ls-files").split())
    # list_repo_files skips dot paths, so .code-indexer is not in it.
    assert tracked == set(list_repo_files(repo)) | {".gitignore"}
    assert _git(repo, "status", "--porcelain") == ""


def test_commit_sync_commits_new_files(tmp_path):
    repo = _repo(tmp_path)
    added = add_files(repo, 3, "sync-001")
    commit_sync(repo, added, "sync-001")
    assert _git(repo, "status", "--porcelain") == ""
    assert set(added) <= set(_git(repo, "ls-files").split())


def test_edit_leaves_unique_uncommitted_changes(tmp_path):
    repo = _repo(tmp_path)
    before = {p: (repo / p).read_bytes() for p in list_repo_files(repo)}
    note = apply_op(repo, "edit", 1, random.Random(0))
    changed = [p for p in before if (repo / p).read_bytes() != before[p]]
    assert len(changed) == 3 and "edit" in note
    assert len(_git(repo, "status", "--porcelain").splitlines()) == 3


def test_rename_commits_moved_files_with_their_content(tmp_path):
    repo = _repo(tmp_path)
    before = sorted((repo / p).read_bytes() for p in list_repo_files(repo))
    apply_op(repo, "rename", 2, random.Random(0))
    assert len([p for p in list_repo_files(repo) if p.startswith("renamed/")]) == 3
    assert sorted((repo / p).read_bytes() for p in list_repo_files(repo)) == before
    assert _git(repo, "status", "--porcelain") == ""


def test_branch_switches_to_a_feature_branch_and_back(tmp_path):
    repo = _repo(tmp_path)
    apply_op(repo, "edit", 1, random.Random(0))
    apply_op(repo, "branch", 2, random.Random(1))
    assert current_branch(repo) == "feature-2"
    assert _git(repo, "status", "--porcelain") == ""
    assert "features/feature-2.md" in list_repo_files(repo)
    apply_op(repo, "branch", 3, random.Random(2))
    assert current_branch(repo) == "main"
    assert "features/feature-2.md" not in list_repo_files(repo)


def test_choose_op_is_seeded():
    a = [choose_op(random.Random(5)) for _ in range(3)]
    b = [choose_op(random.Random(5)) for _ in range(3)]
    assert a == b and set(a) <= set(GIT_OPS)


def test_current_branch_of_a_non_git_directory_is_none(tmp_path):
    assert current_branch(tmp_path) is None
