"""
Regression test for Bug #1578's unconfirmed lead: GitSyncExecutor._execute_git_pull's
"--no-ff" branch (merge_strategy="merge") routes through build_non_interactive_git_env(),
exactly like GitOperationsService.merge_branch(), and `git pull --no-ff` internally
performs a `git merge --no-ff` -- so it was flagged as possibly (but not confirmed)
exposed to the same "Terminal is dumb, but EDITOR unset" / interactive-editor-hang
failure class.

This test drives the REAL, unmocked method against a genuinely diverged pull
(two independent clones of the same remote, each committing something new and
non-conflicting) to force a real merge commit, proving the actual behavior
rather than assuming it from a narrow-scope investigation of merge_branch().

Empirical finding (see tests/unit/server/git/test_git_subprocess_env.py and
tests/unit/services/test_git_merge.py's docstrings for the full investigation):
`git merge` -- and therefore `git pull --no-ff`, which invokes it internally --
only tries to launch an interactive editor when BOTH stdin and stdout are real
ttys. run_git_command() always passes capture_output=True, which pipes stdout
unconditionally, so this call site can never reach that condition. This test
confirms that conclusion for the pull path specifically, with real subprocess
execution and ambient editor variables stripped.
"""

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.git.git_sync_executor import GitSyncExecutor


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _init_repo(path: Path) -> None:
    _run(path, "init", "-b", "master")
    _run(path, "config", "user.email", "test@test.com")
    _run(path, "config", "user.name", "Test")


@pytest.mark.timeout(30)
def test_no_ff_pull_completes_and_creates_merge_commit(tmp_path: Path, monkeypatch):
    """A real, non-fast-forward `git pull --no-ff` must complete cleanly.

    Ambient GIT_EDITOR/GIT_SEQUENCE_EDITOR/EDITOR/VISUAL are stripped so this
    proves GitSyncExecutor's own correctness rather than inheriting a working
    editor from the calling shell's environment.
    """
    for var in ("GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "EDITOR", "VISUAL"):
        monkeypatch.delenv(var, raising=False)

    remote = tmp_path / "remote.git"
    remote.mkdir()
    _run(remote, "init", "--bare", "-b", "master")

    # Seed the remote with an initial commit via a throwaway clone.
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", str(remote), str(seed)], check=True, capture_output=True
    )
    _init_repo(seed)
    (seed / "base.txt").write_text("base content\n")
    _run(seed, "add", ".")
    _run(seed, "commit", "-m", "initial commit")
    _run(seed, "push", "origin", "master")

    # "local" -- the clone under test, operated on via GitSyncExecutor.
    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", str(remote), str(local)], check=True, capture_output=True
    )
    _run(local, "config", "user.email", "test@test.com")
    _run(local, "config", "user.name", "Test")

    # "other" -- a second independent clone that pushes a new commit to the
    # remote AFTER local has already committed its own local-only change, so
    # local's next pull cannot fast-forward.
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(remote), str(other)], check=True, capture_output=True
    )
    _run(other, "config", "user.email", "test@test.com")
    _run(other, "config", "user.name", "Test")

    # Local-only, non-conflicting commit (not yet pushed).
    (local / "local_only.txt").write_text("local content\n")
    _run(local, "add", ".")
    _run(local, "commit", "-m", "local-only commit")

    # Remote-side, non-conflicting commit pushed by "other".
    (other / "remote_only.txt").write_text("remote content\n")
    _run(other, "add", ".")
    _run(other, "commit", "-m", "remote-only commit")
    _run(other, "push", "origin", "master")

    executor = GitSyncExecutor(repository_path=local)
    output = executor._execute_git_pull("merge")

    assert "abort" not in output.lower()

    # A real merge commit must exist: HEAD now has 2 parents.
    parents = subprocess.run(
        ["git", "-C", str(local), "rev-list", "--parents", "-n", "1", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert len(parents.split()) == 3, (
        "Expected HEAD to be a merge commit with 2 parents after "
        f"'git pull --no-ff', got: {parents!r}"
    )
