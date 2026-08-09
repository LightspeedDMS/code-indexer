"""
Unit tests for Bug #1534: file:// golden repo registration must establish a
usable origin/upstream configuration on the CLONE so refresh (GitPullUpdater)
can work regardless of the SOURCE working tree's own git config.

Uses REAL git repos on the real filesystem (no mocks) per this project's
anti-mock rule -- this bug class is exactly what mocks would hide.
"""

import subprocess
from pathlib import Path

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.global_repos.git_pull_updater import GitPullUpdater

GIT_COMMAND_TIMEOUT_SECONDS = 30


def _run_git(args, cwd):
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed in {cwd}: {result.stderr}"
    )
    return result


def _init_repo_with_commit(repo_dir: Path, filename: str = "file.txt") -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _run_git(["init"], repo_dir)
    _run_git(["config", "user.email", "test@example.com"], repo_dir)
    _run_git(["config", "user.name", "Test User"], repo_dir)
    (repo_dir / filename).write_text("initial content\n")
    _run_git(["add", "."], repo_dir)
    _run_git(["commit", "-m", "initial commit"], repo_dir)


def test_a_source_without_origin_gets_origin_configured(tmp_path):
    """
    Source repo has NO origin remote at all (the exact repro in the issue).
    After registration, the clone must have an origin remote and
    GitPullUpdater(clone_path).has_changes() must not raise.
    """
    golden_repos_dir = tmp_path / "golden_repos"
    golden_repos_dir.mkdir()

    source_dir = tmp_path / "source_repo"
    _init_repo_with_commit(source_dir)

    # Confirm precondition: no origin remote on the source.
    remote_result = subprocess.run(
        ["git", "remote"],
        cwd=str(source_dir),
        capture_output=True,
        text=True,
    )
    assert remote_result.returncode == 0
    assert remote_result.stdout.strip() == ""

    manager = GoldenRepoManager(str(golden_repos_dir))
    clone_path = golden_repos_dir / "repos" / "golden-clone"

    repo_url = f"file://{source_dir}"
    result_path = manager._clone_local_repository_with_regular_copy(
        repo_url, str(clone_path)
    )

    assert Path(result_path).exists()

    # Assert the clone now has an origin remote.
    remote_v = subprocess.run(
        ["git", "remote", "-v"],
        cwd=result_path,
        capture_output=True,
        text=True,
    )
    assert remote_v.returncode == 0
    assert "origin" in remote_v.stdout

    # GitPullUpdater must not raise.
    updater = GitPullUpdater(result_path)
    has_changes = updater.has_changes()
    assert has_changes is False


def test_b_end_to_end_refreshability(tmp_path):
    """
    Same setup as (a), then a NEW commit lands on the SOURCE repo. Verify
    has_changes() detects it and update() actually pulls it into the clone.
    """
    golden_repos_dir = tmp_path / "golden_repos"
    golden_repos_dir.mkdir()

    source_dir = tmp_path / "source_repo"
    _init_repo_with_commit(source_dir)

    manager = GoldenRepoManager(str(golden_repos_dir))
    clone_path = golden_repos_dir / "repos" / "golden-clone"

    repo_url = f"file://{source_dir}"
    result_path = manager._clone_local_repository_with_regular_copy(
        repo_url, str(clone_path)
    )

    # New commit on the SOURCE (not the clone).
    new_file = source_dir / "new_file.txt"
    new_file.write_text("brand new content\n")
    _run_git(["add", "."], source_dir)
    _run_git(["commit", "-m", "second commit"], source_dir)

    updater = GitPullUpdater(result_path)
    assert updater.has_changes() is True

    updater.update()

    pulled_file = Path(result_path) / "new_file.txt"
    assert pulled_file.exists()
    assert pulled_file.read_text() == "brand new content\n"


def test_c_source_with_origin_but_no_upstream_gets_fixed_clone(tmp_path):
    """
    Source repo HAS an origin remote configured but NO upstream tracking
    branch (second variant from the issue) -- reproduced via an
    upstream-source bare repo, a source cloned from it, then stripping the
    source's own branch.<name>.merge/remote config to simulate "no upstream
    configured" on the source.

    After registration, the resulting clone's OWN upstream must be correctly
    configured (pointing at origin/main) regardless of the source's broken
    upstream state, and GitPullUpdater(clone_path).has_changes() must not
    raise.
    """
    golden_repos_dir = tmp_path / "golden_repos"
    golden_repos_dir.mkdir()

    upstream_source_dir = tmp_path / "upstream-source.git"
    _run_git(["init", "--bare", str(upstream_source_dir)], tmp_path)

    seed_dir = tmp_path / "seed"
    _init_repo_with_commit(seed_dir)
    _run_git(["push", str(upstream_source_dir), "HEAD:refs/heads/main"], seed_dir)

    source_dir = tmp_path / "source_repo"
    _run_git(["clone", str(upstream_source_dir), str(source_dir)], tmp_path)
    _run_git(["config", "user.email", "test@example.com"], source_dir)
    _run_git(["config", "user.name", "Test User"], source_dir)
    _run_git(["checkout", "main"], source_dir)

    # Simulate "no upstream configured" on the source despite having origin.
    unset_merge = subprocess.run(
        ["git", "config", "--unset", "branch.main.merge"],
        cwd=str(source_dir),
        capture_output=True,
        text=True,
    )
    assert unset_merge.returncode == 0, unset_merge.stderr
    unset_remote = subprocess.run(
        ["git", "config", "--unset", "branch.main.remote"],
        cwd=str(source_dir),
        capture_output=True,
        text=True,
    )
    assert unset_remote.returncode == 0, unset_remote.stderr

    upstream_check = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=str(source_dir),
        capture_output=True,
        text=True,
    )
    assert upstream_check.returncode != 0

    manager = GoldenRepoManager(str(golden_repos_dir))
    clone_path = golden_repos_dir / "repos" / "golden-clone"

    repo_url = f"file://{source_dir}"
    result_path = manager._clone_local_repository_with_regular_copy(
        repo_url, str(clone_path)
    )

    # The clone's OWN upstream must be correctly configured.
    clone_upstream_check = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=result_path,
        capture_output=True,
        text=True,
    )
    assert clone_upstream_check.returncode == 0
    assert clone_upstream_check.stdout.strip() == "origin/main"

    updater = GitPullUpdater(result_path)
    assert updater.has_changes() is False
