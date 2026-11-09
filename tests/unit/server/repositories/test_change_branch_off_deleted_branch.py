"""issue-64616: change_golden_repo_branch moves a repo OFF a branch upstream deleted.

On tools-test three golden repos were registered on branches GitLab no longer
has (their default branch was renamed). ``change_branch`` fetched with the
clone's configured refspec -- on a single-branch clone, the OLD branch -- and
failed ("couldn't find remote ref refs/heads/<old>") before it began. Now:

* only the TARGET branch is fetched (``fetch(ref=target)``; git-mcp's
  ``git_fetch`` ``ref``), it is validated, checked out and pulled;
* afterwards, if the clone's own plain fetch still names a vanished branch,
  ``remote.origin.fetch`` is pointed at the new branch so every later refresh
  fetches again; a full-refspec clone is left alone;
* a refresh that hits the missing ref is classified ``permanent`` and its
  error says what to do.

The git steps of change_branch run against REAL git (local backend) and a
recording git-mcp stand-in (delegated); the rest of change_branch (index,
snapshot, swap) is unchanged and covered elsewhere.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from code_indexer.global_repos.git_error_classifier import (
    classify_fetch_error,
    names_missing_remote_ref,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.storage.shared.git_backend import local_git_backend
from tests.unit.global_repos.test_refresh_git_cancel_2012 import REAL_GIT
from tests.unit.global_repos.test_refresh_registered_branch_2148 import (
    current_branch,
    git_out,
)

needs_git = pytest.mark.skipif(REAL_GIT is None, reason="git binary not installed")

MISSING_REF_STDERR = "fatal: couldn't find remote ref refs/heads/dev\n"


def _manager(backend: Any) -> GoldenRepoManager:
    manager = GoldenRepoManager.__new__(GoldenRepoManager)
    manager._git_backend = backend
    return manager


def _origin_with_renamed_default(tmp_path: Path, *, single_branch: bool) -> Path:
    """An origin whose old default ``dev`` was deleted (``master`` is the new
    default) and a golden clone made while ``dev`` existed."""
    work = tmp_path / "work"
    git_out("init", "-b", "dev", str(work), cwd=tmp_path)
    (work / "a.py").write_text("x = 1\n")
    git_out("add", "-A", cwd=work)
    git_out("commit", "-m", "first", cwd=work)
    git_out("branch", "master", cwd=work)
    origin = tmp_path / "origin.git"
    git_out("clone", "--bare", str(work), str(origin), cwd=tmp_path)
    clone = tmp_path / "golden" / "elasticache"
    clone.parent.mkdir()
    flags = ["--single-branch"] if single_branch else []
    git_out("clone", *flags, "--branch", "dev", str(origin), str(clone), cwd=tmp_path)
    git_out("symbolic-ref", "HEAD", "refs/heads/master", cwd=origin)
    git_out("branch", "-D", "dev", cwd=origin)
    return clone


def _switch(manager: GoldenRepoManager, clone: Path, target: str) -> None:
    """change_branch's git steps, in its order."""
    manager._cb_git_fetch_and_validate(str(clone), target, 60, credentials_url=None)
    manager._cb_checkout_and_pull(str(clone), target, 60, credentials_url=None)


@needs_git
def test_local_single_branch_clone_moves_off_a_deleted_branch(tmp_path: Path) -> None:
    clone = _origin_with_renamed_default(tmp_path, single_branch=True)
    with pytest.raises(subprocess.CalledProcessError) as before:
        git_out("fetch", "origin", cwd=clone)
    assert names_missing_remote_ref(before.value.stderr)

    _switch(_manager(local_git_backend()), clone, "master")

    assert current_branch(clone) == "master"
    assert git_out("config", "--get", "remote.origin.fetch", cwd=clone) == (
        "+refs/heads/master:refs/remotes/origin/master"
    )
    git_out("fetch", "origin", cwd=clone)  # a refresh's fetch works again


@needs_git
def test_local_full_clone_keeps_its_refspec(tmp_path: Path) -> None:
    clone = _origin_with_renamed_default(tmp_path, single_branch=False)

    _switch(_manager(local_git_backend()), clone, "master")

    assert current_branch(clone) == "master"
    assert git_out("config", "--get", "remote.origin.fetch", cwd=clone) == (
        "+refs/heads/*:refs/remotes/origin/*"
    )


@needs_git
def test_a_target_branch_upstream_lacks_is_still_refused(tmp_path: Path) -> None:
    clone = _origin_with_renamed_default(tmp_path, single_branch=True)
    with pytest.raises(ValueError, match="'nope' does not exist on remote"):
        _manager(local_git_backend())._cb_git_fetch_and_validate(
            str(clone), "nope", 60, credentials_url=None
        )
    assert current_branch(clone) == "dev"


# ── delegated (git-mcp) ──────────────────────────────────────────────────────


class _GitMcp:
    """Answers like git-mcp over a single-branch clone of a deleted ``dev``:
    a plain fetch fails with the missing ref, a ``ref`` fetch works."""

    owns_credentials = True

    def __init__(self, *, plain_fetch_error: Optional[str] = MISSING_REF_STDERR):
        self.calls: List[Tuple[str, Any]] = []
        self.plain_fetch_error = plain_fetch_error
        self.fetched_refs: List[str] = []

    def fetch(self, repo_path: str, **kw: Any) -> None:
        ref = kw.get("ref")
        self.calls.append(("fetch", ref))
        if ref is None and self.plain_fetch_error:
            raise subprocess.CalledProcessError(
                128, ["git", "fetch"], stderr=self.plain_fetch_error
            )
        if ref:
            self.fetched_refs.append(ref)

    def list_branches(self, repo_path: str, **kw: Any) -> List[str]:
        return [f"origin/{r}" for r in self.fetched_refs]

    def checkout(self, repo_path: str, ref: str, **kw: Any) -> None:
        self.calls.append(("checkout", ref))

    def pull(self, repo_path: str, **kw: Any) -> None:
        self.calls.append(("pull", kw.get("branch")))

    def config_set(self, repo_path: str, key: str, value: str, **kw: Any) -> None:
        self.calls.append(("config_set", (key, value)))


def test_delegated_change_branch_fetches_only_the_target() -> None:
    git = _GitMcp()
    _switch(_manager(git), Path("/data/golden-repos/epc-api"), "dev")

    assert git.calls == [
        ("fetch", "dev"),
        ("fetch", None),
        (
            "config_set",
            ("remote.origin.fetch", "+refs/heads/dev:refs/remotes/origin/dev"),
        ),
        ("checkout", "dev"),
        ("pull", "dev"),
    ]


def test_delegated_full_clone_is_not_reconfigured() -> None:
    git = _GitMcp(plain_fetch_error=None)
    _switch(_manager(git), Path("/data/golden-repos/tools"), "master")
    assert [c for c in git.calls if c[0] == "config_set"] == []


def test_an_unrelated_post_switch_fetch_failure_does_not_fail_the_change(
    caplog: pytest.LogCaptureFixture,
) -> None:
    git = _GitMcp(plain_fetch_error="fatal: unable to access 'https://x': timeout")
    with caplog.at_level("WARNING"):
        _switch(_manager(git), Path("/data/golden-repos/tools"), "master")
    assert [c for c in git.calls if c[0] == "config_set"] == []
    assert "post-switch fetch" in caplog.text


# ── refresh ──────────────────────────────────────────────────────────────────


def test_a_missing_remote_ref_is_a_permanent_fetch_failure() -> None:
    assert classify_fetch_error(MISSING_REF_STDERR) == "permanent"
    assert names_missing_remote_ref(MISSING_REF_STDERR.encode())
    assert not names_missing_remote_ref("fatal: unable to access")


def test_refresh_error_names_the_registered_branch_and_the_fix() -> None:
    from code_indexer.global_repos.git_error_classifier import GitFetchError
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    scheduler: Any = RefreshScheduler.__new__(RefreshScheduler)
    scheduler._fetch_failure_counts = {}
    scheduler._log_permanent_fetch_failure = lambda *a, **k: None
    scheduler._apply_fetch_backoff = lambda *a, **k: None
    scheduler.golden_repo_metadata = type(
        "M", (), {"get_repo": staticmethod(lambda alias: {"default_branch": "dev"})}
    )()
    error = GitFetchError(
        "fetch failed", category="permanent", stderr=MISSING_REF_STDERR
    )

    with pytest.raises(RuntimeError) as excinfo:
        scheduler._handle_fetch_error("elasticache-global", "u", "/p", error)

    message = str(excinfo.value)
    assert "category=permanent" in message
    assert "registered branch 'dev' no longer exists upstream" in message
    assert "change_golden_repo_branch" in message
