"""
Unit tests for Bug #1534: file:// golden repo registration must establish a
usable origin/upstream configuration on the CLONE so refresh (GitPullUpdater)
can work regardless of the SOURCE working tree's own git config.

Uses REAL git repos on the real filesystem (no mocks) per this project's
anti-mock rule -- this bug class is exactly what mocks would hide.
"""

import logging
import os
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


def test_d_worktree_source_is_detected_and_skipped_safely(tmp_path, caplog):
    """
    Codex review Finding 1: the source path is a real `git worktree add`
    worktree, whose `.git` is a FILE (not a directory) pointing at the main
    repo's `.git/worktrees/<name>` admin dir, which in turn shares its
    `config` + object database with the main repo via `commondir`.

    Empirically confirmed (Bug #1534 investigation): running git mutation
    commands (e.g. `git remote add`) directly against a `shutil.copytree`
    copy of such a worktree writes into the SOURCE main repo's shared
    `.git/config` -- real cross-repository corruption, not a hypothetical.

    The fix must detect this case (a `.git` FILE, not just require a `.git`
    DIRECTORY) and skip remote/upstream setup entirely for it -- never
    attempt to mutate it -- while leaving registration itself successful.
    """
    golden_repos_dir = tmp_path / "golden_repos"
    golden_repos_dir.mkdir()

    main_repo_dir = tmp_path / "main_repo"
    _init_repo_with_commit(main_repo_dir)
    _run_git(["branch", "feature-branch"], main_repo_dir)

    worktree_dir = tmp_path / "the_worktree"
    _run_git(["worktree", "add", str(worktree_dir), "feature-branch"], main_repo_dir)

    # Confirm precondition: `.git` in the worktree is a FILE, not a directory.
    assert (worktree_dir / ".git").is_file()

    main_repo_config_path = main_repo_dir / ".git" / "config"
    config_before = main_repo_config_path.read_bytes()

    manager = GoldenRepoManager(str(golden_repos_dir))
    clone_path = golden_repos_dir / "repos" / "golden-clone-from-worktree"

    repo_url = f"file://{worktree_dir}"
    with caplog.at_level(logging.WARNING):
        result_path = manager._clone_local_repository_with_regular_copy(
            repo_url, str(clone_path)
        )

    # Registration must still succeed.
    assert Path(result_path).exists()

    # No origin remote must have been configured on the clone -- setup must
    # have been skipped, never attempted, for a worktree-sourced clone.
    remote_v = subprocess.run(
        ["git", "remote", "-v"],
        cwd=result_path,
        capture_output=True,
        text=True,
    )
    assert remote_v.returncode == 0
    assert remote_v.stdout.strip() == ""

    # An explicit, worktree-aware warning must have been logged.
    assert any("worktree" in record.message.lower() for record in caplog.records), (
        f"Expected a worktree-aware warning; got: {[r.message for r in caplog.records]}"
    )

    # The SOURCE main repo's shared git config must be byte-identical --
    # no cross-repository mutation occurred.
    config_after = main_repo_config_path.read_bytes()
    assert config_after == config_before


def test_e_exception_handler_itself_never_raises(tmp_path, monkeypatch):
    """
    Codex review Finding 3: the outer `except Exception as e: logging.warning(...)`
    handler in `_establish_local_git_remote_and_upstream` is meant to guarantee
    registration never fails because of this best-effort step. But both
    `str(e)` and the `logging.warning(...)` call itself could theoretically
    raise (e.g. a misconfigured logging handler) -- which would let the
    exception escape the "best-effort" handler and propagate into the
    caller, breaking the very contract this step exists to uphold.

    Forces BOTH: an exception inside the try block, via the external
    `subprocess.run` boundary (monkeypatched to raise -- never the method
    under test itself) so the first real git call inside
    `_establish_local_git_remote_and_upstream` blows up; AND a raising
    `logging.warning`. Calls `_establish_local_git_remote_and_upstream`
    directly and asserts it does not raise despite both failures.
    """
    import code_indexer.server.repositories.golden_repo_manager as grm_module

    golden_repos_dir = tmp_path / "golden_repos"
    golden_repos_dir.mkdir()

    source_dir = tmp_path / "source_repo"
    _init_repo_with_commit(source_dir)

    manager = GoldenRepoManager(str(golden_repos_dir))
    clone_path = golden_repos_dir / "repos" / "golden-clone"
    clone_path.parent.mkdir(parents=True, exist_ok=True)

    # Real filesystem copy of a real git repo -- `.git` is a genuine
    # directory, so the worktree-skip branch (Finding 1) is not taken and
    # execution reaches the first real git subprocess call.
    subprocess.run(
        ["cp", "-a", str(source_dir), str(clone_path)],
        check=True,
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
    )
    assert (clone_path / ".git").is_dir()

    def _raise_subprocess_run(*args, **kwargs):
        raise RuntimeError("simulated failure inside a git subprocess call")

    def _raise_logging_warning(*args, **kwargs):
        raise RuntimeError("simulated failure inside logging.warning itself")

    monkeypatch.setattr(grm_module.subprocess, "run", _raise_subprocess_run)
    monkeypatch.setattr(grm_module.logging, "warning", _raise_logging_warning)

    # Must not raise despite both the subprocess-call exception AND the
    # logging call itself raising.
    manager._establish_local_git_remote_and_upstream(
        str(clone_path), os.path.realpath(str(source_dir))
    )
