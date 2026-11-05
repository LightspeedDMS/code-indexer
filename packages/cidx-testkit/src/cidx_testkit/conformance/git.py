"""Conformance suite for the git seam (``cidx_sdk.seams.git``).

Every git backend -- the local one, each delegate, the testkit's fake git-mcp
-- must pass the same checks. Subclass :class:`GitBackendContract` and provide
the ``git_subject`` fixture, a :class:`GitSubject`::

    class TestLocalGitBackend(GitBackendContract):
        @pytest.fixture
        def git_subject(self, tmp_path):
            return local_subject(make_git_backend(runner), tmp_path)

The suite works only through the backend: it clones the subject's upstream
into the subject's work directory and asks every question through the
Protocol, so a backend that holds its repositories elsewhere (git-mcp) is
checked exactly like one that holds them here. The upstream is the remote
side: the suite advances it directly (:meth:`GitSubject.advance_upstream`).

What the core relies on, and so what is checked: failures RAISE
``CalledProcessError`` (except where the Protocol says the exit is returned),
a detached HEAD is reported by ``symbolic_ref`` with
:data:`~cidx_sdk.seams.git.DETACHED_HEAD_MARKER` in stderr, history comes back
as BYTES, and a backend that also implements ``GitCommandRunner`` returns a
non-zero tool exit instead of raising and attributes commits to the identity
it is given.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from cidx_sdk.seams.git import (
    DETACHED_HEAD_MARKER,
    GitBackend,
    GitCommandRunner,
    Timeout,
)

#: The upstream's default branch, and its second branch.
MAIN = "main"
FEATURE = "feature"
#: Bytes of ``a.txt`` at the upstream's HEAD.
A_CONTENT = b"one\ntwo\n"

_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "Example",
    "GIT_AUTHOR_EMAIL": "example@example.com",
    "GIT_COMMITTER_NAME": "Example",
    "GIT_COMMITTER_EMAIL": "example@example.com",
}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_IDENTITY_ENV, "GIT_TERMINAL_PROMPT": "0"},
    )
    return done.stdout


def seed_upstream(path: Path) -> Path:
    """A repository at *path* with two commits on ``main`` (``a.txt``, then
    ``a.txt`` changed and ``b.txt`` added) and a ``feature`` branch one commit
    ahead (``c.txt``), checked out on ``main``. Needs ``git`` on PATH."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", MAIN)
    (path / "a.txt").write_bytes(b"one\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "first")
    (path / "a.txt").write_bytes(A_CONTENT)
    (path / "b.txt").write_bytes(b"bee\n")
    _git(path, "add", "a.txt", "b.txt")
    _git(path, "commit", "-q", "-m", "second")
    _git(path, "checkout", "-q", "-b", FEATURE)
    (path / "c.txt").write_bytes(b"sea\n")
    _git(path, "add", "c.txt")
    _git(path, "commit", "-q", "-m", "feature")
    _git(path, "checkout", "-q", MAIN)
    return path


class PlainGitProcessRunner:
    """A ``GitProcessRunner`` for testing a local backend without the core:
    ``subprocess.run`` with a fixed commit identity and no prompts. The core
    hands the real backend its own runner (SSH-safe environment,
    credentials, cancellation); this one only runs git."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def run_git(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout: Timeout = None,
        retry: bool = False,
        text: bool = True,
        credentials_url: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> "subprocess.CompletedProcess[Any]":
        self.calls.append(
            {"cmd": list(cmd), "cwd": cwd, "retry": retry, "env": dict(env or {})}
        )
        return subprocess.run(
            list(cmd),
            cwd=cwd,
            check=check,
            capture_output=True,
            text=text,
            timeout=timeout,
            env={
                **os.environ,
                **_IDENTITY_ENV,
                "GIT_TERMINAL_PROMPT": "0",
                **(env or {}),
            },
        )


@dataclass
class GitSubject:
    """A backend under test and the repositories it is checked with.

    Attributes:
        backend: the implementation under test.
        workdir: a directory, as the BACKEND addresses it, the suite clones
            into (``<workdir>/<name>``); it exists and is empty.
        upstream: the URL the backend clones from (a repository laid out by
            :func:`seed_upstream`).
        upstream_dir: that repository on this machine, so the suite can
            advance it the way a push from elsewhere would.
    """

    backend: GitBackend
    workdir: str
    upstream: str
    upstream_dir: Path

    def advance_upstream(self, name: str = "d.txt") -> str:
        """Commit a new file on the upstream's ``main``; its new HEAD sha."""
        (self.upstream_dir / name).write_bytes(b"later\n")
        _git(self.upstream_dir, "add", name)
        _git(self.upstream_dir, "commit", "-q", "-m", f"add {name}")
        return _git(self.upstream_dir, "rev-parse", "HEAD").strip()


def local_subject(backend: GitBackend, tmp_path: Path) -> GitSubject:
    """A subject whose work directory and upstream are on this machine."""
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    upstream = seed_upstream(tmp_path / "upstream")
    return GitSubject(backend, str(workdir), str(upstream), upstream)


class GitBackendContract:
    """The git seam's contract. Subclass and provide ``git_subject``."""

    @pytest.fixture
    def git_subject(self, tmp_path: Path) -> GitSubject:
        raise NotImplementedError("provide the git backend under test")

    @pytest.fixture
    def clone(self, git_subject: GitSubject) -> str:
        """The upstream cloned through the backend; its path as addressed."""
        dest = f"{git_subject.workdir}/clone"
        git_subject.backend.clone(git_subject.upstream, dest, timeout=60)
        return dest

    # -- shape -----------------------------------------------------------------

    def test_satisfies_the_protocol(self, git_subject: GitSubject) -> None:
        assert isinstance(git_subject.backend, GitBackend)
        assert isinstance(git_subject.backend.owns_credentials, bool)

    # -- remote-URL operations -------------------------------------------------

    def test_ls_remote_tells_a_reachable_remote_from_a_missing_one(
        self, git_subject: GitSubject
    ) -> None:
        backend = git_subject.backend
        assert backend.ls_remote(git_subject.upstream, timeout=60) is True
        missing = f"{git_subject.upstream_dir.parent}/no-such-repository"
        assert backend.ls_remote(missing, timeout=60) is False

    def test_clone_checks_out_the_default_branch(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        assert git_subject.backend.current_branch(clone) == MAIN

    def test_clone_of_a_named_branch(self, git_subject: GitSubject) -> None:
        dest = f"{git_subject.workdir}/feature"
        git_subject.backend.clone(git_subject.upstream, dest, branch=FEATURE)
        assert git_subject.backend.current_branch(dest) == FEATURE

    def test_clone_onto_an_existing_repository_raises(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        with pytest.raises(subprocess.CalledProcessError):
            git_subject.backend.clone(git_subject.upstream, clone)

    # -- inspection ------------------------------------------------------------

    def test_rev_parse_and_a_failure_raises(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        backend = git_subject.backend
        head = backend.rev_parse(clone, "HEAD")
        assert len(head) == 40 and int(head, 16) >= 0
        with pytest.raises(subprocess.CalledProcessError):
            backend.rev_parse(clone, "--verify", "no-such-ref")

    def test_rev_list_count(self, git_subject: GitSubject, clone: str) -> None:
        assert git_subject.backend.rev_list_count(clone, "HEAD") == 2
        assert git_subject.backend.rev_list_count(clone, f"HEAD..origin/{FEATURE}") == 1

    def test_a_detached_head_is_reported_with_the_marker(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        backend = git_subject.backend
        assert backend.symbolic_ref(clone, short=True) == MAIN
        assert backend.symbolic_ref(clone, short=False) == f"refs/heads/{MAIN}"
        backend.checkout(clone, backend.rev_parse(clone, "HEAD~1"))
        with pytest.raises(subprocess.CalledProcessError) as raised:
            backend.symbolic_ref(clone)
        assert DETACHED_HEAD_MARKER in str(raised.value.stderr)

    def test_list_branches(self, git_subject: GitSubject, clone: str) -> None:
        backend = git_subject.backend
        assert backend.list_branches(clone, fmt="%(refname:short)") == [MAIN]
        remote = backend.list_branches(clone, remote=True)
        assert any(line.endswith(f"origin/{FEATURE}") for line in remote)
        every = backend.list_branches(clone, all_refs=True, fmt="%(refname)")
        assert f"refs/heads/{MAIN}" in every
        assert f"refs/remotes/origin/{FEATURE}" in every

    def test_checkout_creates_and_switches_branches(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        backend = git_subject.backend
        backend.checkout(clone, "work", create=True)
        assert backend.current_branch(clone) == "work"
        backend.checkout(clone, "work", force_new=True, start_point="HEAD~1")
        assert backend.rev_list_count(clone, "HEAD") == 1
        backend.checkout(clone, MAIN)
        assert backend.current_branch(clone) == MAIN
        with pytest.raises(subprocess.CalledProcessError):
            backend.checkout(clone, "no-such-branch")

    def test_working_tree_questions(self, git_subject: GitSubject, clone: str) -> None:
        backend = git_subject.backend
        assert backend.status_porcelain(clone).strip() == ""
        assert backend.ls_files(clone) == ["a.txt", "b.txt"]
        changed = backend.diff_name_only(clone, f"HEAD..origin/{FEATURE}")
        assert changed == ["c.txt"]

    def test_show_ref_returns_a_missing_ref_rather_than_raising(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        backend = git_subject.backend
        found = backend.show_ref(clone, "--verify", f"refs/heads/{MAIN}")
        assert found.returncode == 0
        missing = backend.show_ref(clone, "--verify", "refs/heads/no-such-branch")
        assert missing.returncode != 0

    # -- history ---------------------------------------------------------------

    def test_log_raw_is_bytes_and_honours_its_limits(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        backend = git_subject.backend
        full = backend.log_raw(clone, "%H")
        assert isinstance(full, bytes) and len(full.split()) == 2
        assert len(backend.log_raw(clone, "%H", max_count=1).split()) == 1
        assert len(backend.log_raw(clone, "%H", skip=1).split()) == 1
        every = backend.log_raw(clone, "%s", all_refs=True)
        assert b"feature" in every.split(b"\n")
        assert backend.log_raw(clone, "%s", [f"origin/{FEATURE}"]).startswith(
            b"feature"
        )

    def test_for_each_ref_is_bytes(self, git_subject: GitSubject, clone: str) -> None:
        refs = git_subject.backend.for_each_ref(
            clone, "%(refname)", ["refs/remotes/origin"], sort="refname"
        )
        assert isinstance(refs, bytes)
        assert f"refs/remotes/origin/{FEATURE}".encode() in refs.split(b"\n")
        one = git_subject.backend.for_each_ref(
            clone, "%(refname)", ["refs/remotes/origin"], count=1
        )
        assert len(one.split()) == 1

    def test_diff_commits_is_one_patch_per_pair(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        backend = git_subject.backend
        first = backend.rev_parse(clone, "HEAD~1")
        second = backend.rev_parse(clone, "HEAD")
        patches = backend.diff_commits(clone, [(first, second)], 3)
        assert len(patches) == 1 and isinstance(patches[0], bytes)
        assert b"b.txt" in patches[0] and b"+two" in patches[0]

    def test_show_objects_returns_each_result_without_raising(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        shown = git_subject.backend.show_objects(
            clone, [("HEAD", "a.txt"), ("HEAD", "missing.txt")]
        )
        assert [result.returncode == 0 for result in shown] == [True, False]
        assert shown[0].stdout == A_CONTENT

    # -- fetch / pull / merge --------------------------------------------------

    def test_fetch_then_merge_brings_in_upstream_work(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        backend = git_subject.backend
        new_head = git_subject.advance_upstream()
        backend.fetch(clone, timeout=60)
        assert backend.rev_parse(clone, f"origin/{MAIN}") == new_head
        backend.merge(clone, f"origin/{MAIN}")
        assert backend.rev_parse(clone, "HEAD") == new_head

    def test_fetch_of_one_ref(self, git_subject: GitSubject, clone: str) -> None:
        backend = git_subject.backend
        new_head = git_subject.advance_upstream()
        backend.fetch(clone, "origin", ref=MAIN, timeout=60)
        assert backend.rev_parse(clone, f"origin/{MAIN}") == new_head

    def test_pull_fast_forwards(self, git_subject: GitSubject, clone: str) -> None:
        backend = git_subject.backend
        new_head = git_subject.advance_upstream()
        backend.pull(clone, "origin", MAIN, mode="ff-only", timeout=60)
        assert backend.rev_parse(clone, "HEAD") == new_head

    def test_pull_from_a_missing_remote_raises(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        with pytest.raises(subprocess.CalledProcessError):
            git_subject.backend.pull(clone, "no-such-remote", MAIN)

    # -- mutation --------------------------------------------------------------

    def test_reset_moves_head(self, git_subject: GitSubject, clone: str) -> None:
        backend = git_subject.backend
        backend.reset(clone, "hard", "HEAD~1")
        assert backend.rev_list_count(clone, "HEAD") == 1
        assert backend.ls_files(clone) == ["a.txt"]
        backend.update_index_refresh(clone)
        backend.restore(clone, ".")
        assert backend.status_porcelain(clone).strip() == ""

    def test_config_set_is_read_back(self, git_subject: GitSubject, clone: str) -> None:
        backend = git_subject.backend
        backend.config_set(clone, "cidx.example", "value")
        if isinstance(backend, GitCommandRunner):
            done = backend.run(clone, ["config", "--get", "cidx.example"])
            assert done.stdout.strip() == "value"

    # -- remotes ---------------------------------------------------------------

    def test_remote_lifecycle(self, git_subject: GitSubject, clone: str) -> None:
        backend = git_subject.backend
        assert backend.remote_list(clone).split() == ["origin"]
        backend.remote_add(clone, "mirror", "https://example.com/a.git")
        assert backend.remote_get_url(clone, "mirror") == "https://example.com/a.git"
        backend.remote_set_url(clone, "mirror", "https://example.com/b.git")
        assert backend.remote_get_url(clone, "mirror") == "https://example.com/b.git"
        backend.remote_rename(clone, "mirror", "backup")
        assert "backup\thttps://example.com/b.git (fetch)" in backend.remote_list(
            clone, verbose=True
        )
        backend.remote_remove(clone, "backup")
        assert backend.remote_list(clone).split() == ["origin"]
        with pytest.raises(subprocess.CalledProcessError):
            backend.remote_get_url(clone, "backup")

    # -- the git tools (GitCommandRunner) --------------------------------------

    def _runner(self, git_subject: GitSubject) -> GitCommandRunner:
        backend = git_subject.backend
        if not isinstance(backend, GitCommandRunner):
            pytest.skip("this backend does not carry the git tools")
        return backend

    def test_a_tool_failure_is_returned_not_raised(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        done = self._runner(git_subject).run(clone, ["rev-parse", "no-such-ref"])
        assert isinstance(done, subprocess.CompletedProcess)
        assert done.returncode != 0 and isinstance(done.stderr, str)

    def test_a_commit_is_attributed_to_the_given_identity(
        self, git_subject: GitSubject, clone: str
    ) -> None:
        identity: Dict[str, str] = {
            "author_name": "Author Example",
            "author_email": "author@example.com",
            "committer_name": "Committer Example",
            "committer_email": "committer@example.com",
        }
        runner = self._runner(git_subject)
        done = runner.run(
            clone, ["commit", "--allow-empty", "-q", "-m", "tool"], identity=identity
        )
        assert done.returncode == 0, done.stderr
        line = git_subject.backend.log_raw(clone, "%an <%ae>|%cn <%ce>", max_count=1)
        assert line.strip() == (
            b"Author Example <author@example.com>|"
            b"Committer Example <committer@example.com>"
        )

    def test_push_publishes_a_branch(self, git_subject: GitSubject, clone: str) -> None:
        runner = self._runner(git_subject)
        git_subject.backend.checkout(clone, "pushed", create=True)
        runner.push(clone, "origin", "pushed", timeout=60)
        heads: List[str] = _git(git_subject.upstream_dir, "branch").split()
        assert "pushed" in heads
        with pytest.raises(subprocess.CalledProcessError):
            runner.push(clone, "no-such-remote", "pushed")
