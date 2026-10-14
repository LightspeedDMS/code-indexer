"""cidx's git tools on an activated copy that is not on this pod.

Under ``content_backend='delegated'`` a user's activated working copy exists
only on the shared volume, at the path cidx computes for it, which does NOT
exist on this machine in these tests. ``GitOperationsService`` then runs its
git through the git seam -- :class:`FakeGitMcp`, which runs real git on the
REMOTE tree addressed by the POD path, exactly as git-mcp's ``git_exec`` /
``git_push`` / ``git_pull`` / ``git_fetch`` do -- and reads working-tree files
(conflict markers) through the content seam.

Every tool runs twice over identical repositories -- locally (today's code,
no seam anywhere) and delegated on the absent pod path -- and the results and
resulting repositories must be identical. The pod path never appearing is the
mount-independence proof.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pytest

from code_indexer.server.services import git_operations_service as gos_mod
from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    git_operations_service,
)
from code_indexer.server.startup import content_backend_wiring as backend_wiring
from code_indexer.server.startup import git_backend_wiring
from code_indexer.server.storage.shared.content_backend import LocalContentBackend
from code_indexer.server.storage.shared.git_backend import LocalGitBackend

_DATES = {
    "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z",
}


def _load_git_mcp_plugin() -> Any:
    """The git-mcp plugin (its own distribution, not installed with cidx),
    loaded from the repository so the fake drives its real translation."""
    path = (
        Path(__file__).resolve().parents[4]
        / "plugins"
        / "cidx_git_backend"
        / "cidx_git_backend"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("_cidx_git_backend_plugin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PLUGIN = _load_git_mcp_plugin()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class _Map:
    """Pod path -> the remote tree that holds it (and remote URLs back)."""

    def __init__(self, pod: Path, remote: Path) -> None:
        self.pod = PurePosixPath(pod)
        self.remote = remote

    def __call__(self, value: Any) -> Any:
        if isinstance(value, (str, Path)):
            text = str(value)
            root = str(self.pod)
            if text == root or text.startswith(root + "/"):
                rel = PurePosixPath(text).relative_to(self.pod)
                return (
                    str(self.remote / str(rel)) if str(rel) != "." else str(self.remote)
                )
        return value


class FakeGitMcp:
    """git-mcp: git_exec (run), git_push/pull/fetch, by POD path."""

    owns_credentials = True

    def __init__(self, mapping: _Map) -> None:
        self._map = mapping
        self._git = LocalGitBackend()
        self.calls: List[Tuple[str, List[str]]] = []
        self.set_upstream: List[bool] = []
        self.fail_run: Optional[Exception] = None

    def run(
        self,
        repo_path: str,
        args: Sequence[str],
        timeout: Any = None,
        identity: Optional[Dict[str, str]] = None,
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append(("exec", list(args)))
        if self.fail_run is not None:
            raise self.fail_run
        if args and args[0] in ("push", "pull", "fetch", "clone"):
            raise AssertionError(f"git_exec refuses network: {args}")
        result: "subprocess.CompletedProcess[str]" = self._git.run(
            self._map(repo_path), list(args), timeout=timeout, identity=identity
        )
        return result

    def push(
        self,
        repo_path: str,
        remote: str,
        branch: Optional[str] = None,
        timeout: Any = None,
        set_upstream: bool = False,
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append(("push", [remote, branch or ""]))
        self.set_upstream.append(set_upstream)
        return self._git.push(
            self._map(repo_path),
            remote,
            branch,
            timeout=timeout,
            set_upstream=set_upstream,
        )

    def pull(
        self,
        repo_path: str,
        remote: Optional[str] = None,
        branch: Optional[str] = None,
        **kw: Any,
    ) -> None:
        """The REAL plugin's ``pull`` over :meth:`_git_mcp_pull`: what cidx
        sees is the plugin's translation of git-mcp's result, not git's own
        output (which git-mcp never forwards)."""
        self.calls.append(("pull", [remote or "", branch or ""]))
        backend = _PLUGIN.GitMcpBackend(base_url="http://git-mcp.invalid")
        setattr(
            backend,
            "_call",
            lambda tool, arguments, timeout=None: self._git_mcp_pull(arguments),
        )
        backend.pull(repo_path, remote=remote, branch=branch)

    def _git_mcp_pull(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """git-mcp's ``gitPull`` (git-api.service.ts): ``git pull``; on failure
        a conflicted tree is RETURNED as ``{status: 'conflicts'}`` and any other
        failure is thrown -- a tool error, git's stderr as its text."""
        repo = self._map(arguments["cow_path"])
        argv = [arguments.get("remote"), arguments.get("branch")]
        pulled = subprocess.run(
            ["git", "pull", *[a for a in argv if a]],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if pulled.returncode == 0:
            return {"status": "merged", "commits_pulled": 1, "conflicts": None}
        conflicted = _git(Path(repo), "diff", "--name-only", "--diff-filter=U")
        if conflicted:
            return {
                "status": "conflicts",
                "commits_pulled": 0,
                "conflicts": [
                    {"path": path, "conflict_type": "both_modified"}
                    for path in conflicted.splitlines()
                ],
            }
        raise _PLUGIN.GitMcpBackendError(
            f"git-mcp git_pull tool error: {pulled.stderr}", stderr=pulled.stderr
        )

    def fetch(self, repo_path: str, remote: str = "origin", **kw: Any) -> None:
        self.calls.append(("fetch", [remote]))
        self._git.fetch(self._map(repo_path), remote)


class FakeContent:
    """A NON-local content backend over the remote tree."""

    def __init__(self, mapping: _Map) -> None:
        self._map = mapping
        self._inner = LocalContentBackend()

    @property
    def is_local(self) -> bool:
        return False

    def manifest(self, repo_path: Any, **kwargs: Any) -> Any:
        return self._inner.manifest(self._map(repo_path), **kwargs)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Any:
        return self._inner.fetch(self._map(repo_path), paths)


def _make_repo(root: Path, upstream: Path) -> Path:
    repo = root / "repo"
    subprocess.run(["git", "clone", "-q", str(upstream), str(repo)], check=True)
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Example")
    _git(repo, "checkout", "-q", "-b", "work")
    return repo


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    for key, value in _DATES.items():
        monkeypatch.setenv(key, value)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "a.txt").write_text("one\ntwo\n")
    (seed / "b.txt").write_text("bee\n")
    _git(seed, "add", ".")
    _git(
        seed,
        "-c",
        "user.email=s@example.com",
        "-c",
        "user.name=S",
        "commit",
        "-qm",
        "init",
    )
    upstream = tmp_path / "upstream.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(upstream)], check=True
    )

    local = _make_repo(tmp_path / "local", upstream)
    remote = _make_repo(tmp_path / "remote", upstream)
    pod = tmp_path / "not-mounted" / "activated-repos" / "alice" / "repo"
    mapping = _Map(pod, remote)
    w = SimpleNamespace(
        local=local,
        remote=remote,
        pod=pod,
        upstream=upstream,
        git=FakeGitMcp(mapping),
        content=FakeContent(mapping),
        monkeypatch=monkeypatch,
    )
    _local(w)
    return w


def _local(w: SimpleNamespace) -> None:
    w.monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: None)
    w.monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: None
    )


def _delegate(w: SimpleNamespace, git: Any = None) -> None:
    w.monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: w.content)
    chosen = w.git if git is None else git
    w.monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: chosen
    )


def _state(repo: Path) -> Dict[str, str]:
    """The repository as git sees it (HEAD, branches, index, worktree, stashes)."""
    files = {
        str(p.relative_to(repo)): p.read_text(errors="replace")
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }
    return {
        "head": _git(repo, "rev-parse", "HEAD"),
        "branch": _git(repo, "branch", "--show-current"),
        "branches": _git(
            repo, "branch", "-a", "--format=%(refname:short) %(objectname)"
        ),
        "status": _git(repo, "status", "--porcelain"),
        "stash": _git(repo, "stash", "list", "--format=%gs"),
        "files": repr(files),
    }


def _outcome(call: Callable[[], Any]) -> Tuple[str, Any]:
    try:
        return "ok", call()
    except Exception as exc:  # the outcome under test, whatever it is
        return type(exc).__name__, str(exc).replace("\\n", " ")


def _dirty(repo: Path) -> bool:
    """Modify a tracked file and add an untracked one (True: for chaining)."""
    (repo / "a.txt").write_text("one\nTWO\n")
    (repo / "c.txt").write_text("new\n")
    return True


svc = git_operations_service

SCENARIOS: Dict[str, Callable[[Path, Path], Any]] = {
    "status": lambda r, t: (_dirty(t), svc.git_status(r))[1],
    "diff": lambda r, t: (_dirty(t), svc.git_diff(r))[1],
    "log": lambda r, t: svc.git_log(r, limit=5),
    "stage_commit": lambda r, t: (
        _dirty(t),
        svc.git_stage(r, ["a.txt", "c.txt"]),
        svc.git_commit(r, "change", "alice@example.com", "Alice"),
    )[2],
    "unstage": lambda r, t: (
        _dirty(t),
        svc.git_stage(r, ["a.txt"]),
        svc.git_unstage(r, ["a.txt"]),
    )[2],
    "branch_list": lambda r, t: svc.git_branch_list(r),
    "branch_create_switch": lambda r, t: (
        svc.git_branch_create(r, "feature"),
        svc.git_branch_switch(r, "feature"),
    )[1],
    "checkout_file": lambda r, t: (_dirty(t), svc.git_checkout_file(r, "a.txt"))[1],
    "stash": lambda r, t: (
        _dirty(t),
        svc.git_stash_push(r, message="wip"),
        svc.git_stash_list(r),
    )[2],
    "fetch": lambda r, t: svc.git_fetch(r, "origin"),
    "pull": lambda r, t: svc.git_pull(r, "origin", "main"),
    "push": lambda r, t: svc.git_push(r, "origin", "work"),
    "push_unknown_remote": lambda r, t: svc.git_push(r, "nope", "work"),
}

#: Results that differ only in what git prints about the transport: the
#: delegated push reports git-mcp's summary instead of git's own stderr.
_STATE_ONLY = frozenset({"push"})


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_each_git_tool_is_identical_local_and_delegated(
    world: SimpleNamespace, name: str
) -> None:
    call = SCENARIOS[name]
    local = _outcome(lambda: call(world.local, world.local))

    _delegate(world)
    delegated = _outcome(lambda: call(world.pod, world.remote))

    if name in _STATE_ONLY:
        assert delegated[0] == local[0]
    else:
        assert delegated == local
    assert _state(world.remote) == _state(world.local)
    assert not world.pod.exists(), "a delegated git tool touched this pod's disk"
    assert world.git.calls, "the delegated run never reached the git seam"


def test_a_commit_carries_its_author_and_committer(world: SimpleNamespace) -> None:
    _delegate(world)
    _dirty(world.remote)
    svc.git_stage(world.pod, ["a.txt"])
    svc.git_commit(
        world.pod,
        "attributed",
        "alice@example.com",
        "Alice",
        committer_email="bot@example.com",
        committer_name="Bot",
    )
    assert _git(world.remote, "log", "-1", "--format=%an|%ae|%cn|%ce") == (
        "Alice|alice@example.com|Bot|bot@example.com"
    )


def test_network_operations_use_the_structured_tools(world: SimpleNamespace) -> None:
    _delegate(world)
    svc.git_fetch(world.pod, "origin")
    svc.git_push(world.pod, "origin", "work")
    kinds = [kind for kind, _ in world.git.calls]
    assert "fetch" in kinds and "push" in kinds
    assert not any(
        kind == "exec" and args[0] in ("push", "fetch")
        for kind, args in world.git.calls
    )
    assert _git(world.upstream, "branch", "--list", "work") == "work"


def test_pat_push_uses_git_mcp_credentials_not_cidx_s(world: SimpleNamespace) -> None:
    """Delegated: the PAT never leaves cidx; git-mcp pushes with its own."""
    _delegate(world)
    result = svc.git_push_with_pat(
        world.pod, "origin", "work", {"token": "unused", "git_user_name": "A"}
    )
    assert result["success"] is True
    assert ("push", ["origin", "work"]) in world.git.calls


def _advance_upstream(world: SimpleNamespace, name: str, text: str) -> None:
    """One new commit on the upstream's main (both copies pull the same one)."""
    scratch = world.upstream.parent / "upstream-writer"
    subprocess.run(
        ["git", "clone", "-q", str(world.upstream), str(scratch)], check=True
    )
    (scratch / name).write_text(text)
    _git(
        scratch,
        "-c",
        "user.email=u@example.com",
        "-c",
        "user.name=U",
        "commit",
        "-qam",
        "upstream",
    )
    _git(scratch, "push", "-q", "origin", "main")


def test_a_pull_reports_the_files_it_changed_like_local(
    world: SimpleNamespace,
) -> None:
    _advance_upstream(world, "b.txt", "BEE\n")
    local = svc.git_pull(world.local, "origin", "main")

    _delegate(world)
    delegated = svc.git_pull(world.pod, "origin", "main")

    assert local == {"success": True, "updated_files": 1, "conflicts": []}
    assert delegated == local
    assert _state(world.remote) == _state(world.local)


def test_a_conflicting_pull_reports_its_conflicts_like_local(
    world: SimpleNamespace,
) -> None:
    """git-mcp RETURNS a conflicted pull as {status: 'conflicts'}; it must reach
    the caller as a failed pull with the conflicted paths, not as success."""
    _advance_upstream(world, "a.txt", "one\nUPSTREAM\n")
    for repo in (world.local, world.remote):
        # git >= 2.27 refuses a divergent pull until told how to reconcile.
        _git(repo, "config", "pull.rebase", "false")
        (repo / "a.txt").write_text("one\nWORK\n")
        _git(repo, "commit", "-qam", "work")
    local = svc.git_pull(world.local, "origin", "main")

    _delegate(world)
    delegated = svc.git_pull(world.pod, "origin", "main")

    assert local == {"success": False, "updated_files": 0, "conflicts": ["a.txt"]}
    assert delegated == local
    assert svc.git_conflict_status(world.pod)["in_merge"] is True
    assert not world.pod.exists()


def test_a_failed_pull_that_is_not_a_conflict_still_fails(
    world: SimpleNamespace,
) -> None:
    _delegate(world)
    result = svc.git_pull(world.pod, "origin", "no-such-branch")
    assert result["success"] is False and result["conflicts"] == []


def test_conflict_text_only_in_the_error_still_names_the_conflicts(
    world: SimpleNamespace,
) -> None:
    """A backend that raises with git's CONFLICT lines only in stderr."""
    from code_indexer.server.services.git_tools_seam import DelegatedGitTools

    class StderrOnly:
        def run(self, repo: str, args: Sequence[str], **kw: Any) -> Any:
            return subprocess.CompletedProcess(args, 0, "", "")

        def pull(self, repo: str, **kw: Any) -> None:
            raise subprocess.CalledProcessError(
                1,
                "git-mcp",
                output=None,
                stderr="Auto-merging a.txt\nCONFLICT (content): Merge conflict in a.txt\n",
            )

    result = DelegatedGitTools(StderrOnly()).run(
        ["git", "pull", "origin", "main"], world.pod, check=False
    )
    assert result.returncode == 1
    assert result.stdout == "CONFLICT (content): Merge conflict in a.txt\n"


def test_diffstat_failure_after_a_successful_pull_still_reports_success(
    world: SimpleNamespace,
) -> None:
    """A transport error on the follow-up ``git diff --shortstat`` must not
    mask a pull that already succeeded."""
    from code_indexer.server.services.git_tools_seam import DelegatedGitTools

    class DiffstatFails:
        def run(self, repo: str, args: Sequence[str], **kw: Any) -> Any:
            if args and args[0] == "rev-parse":
                return subprocess.CompletedProcess(args, 0, "abc123\n", "")
            raise subprocess.CalledProcessError(1, "git-mcp", output=None, stderr="transport error")

        def pull(self, repo: str, **kw: Any) -> None:
            pass

    result = DelegatedGitTools(DiffstatFails()).run(
        ["git", "pull", "origin", "main"], world.pod, check=False
    )
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize("set_upstream", [True, False])
def test_pat_push_sets_upstream_only_when_asked(
    world: SimpleNamespace, set_upstream: bool
) -> None:
    """git-mcp's git_push tracks by default; the seam asks for exactly what
    the caller asked for."""
    _delegate(world)
    svc.git_push_with_pat(
        world.pod, "origin", "work", {"token": "unused"}, set_upstream=set_upstream
    )
    assert world.git.set_upstream == [set_upstream]
    tracked = subprocess.run(
        ["git", "config", "--get", "branch.work.remote"],
        cwd=world.remote,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert tracked == ("origin" if set_upstream else "")


def test_a_plain_push_does_not_set_upstream(world: SimpleNamespace) -> None:
    _delegate(world)
    svc.git_push(world.pod, "origin", "work")
    assert world.git.set_upstream == [False]


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "push", "--force", "origin", "work"],
        ["git", "push", "--end-of-options", "-u", "origin"],
        ["git", "pull", "--set-upstream", "origin", "main"],
        ["git", "fetch", "-u", "origin"],
    ],
)
def test_other_network_options_are_refused(
    world: SimpleNamespace, argv: List[str]
) -> None:
    from code_indexer.server.services.git_tools_seam import (
        DelegatedGitTools,
        GitToolsUnavailableError,
    )

    with pytest.raises(GitToolsUnavailableError, match="with options"):
        DelegatedGitTools(world.git).run(argv, world.pod)
    assert world.git.calls == []


def test_conflicts_are_read_through_the_seams(world: SimpleNamespace) -> None:
    def make_conflict(repo: Path) -> None:
        _git(repo, "checkout", "-q", "-b", "other")
        (repo / "a.txt").write_text("one\nOTHER\n")
        _git(repo, "commit", "-qam", "other")
        _git(repo, "checkout", "-q", "work")
        (repo / "a.txt").write_text("one\nWORK\n")
        _git(repo, "commit", "-qam", "work")

    make_conflict(world.local)
    make_conflict(world.remote)
    local_merge = _outcome(lambda: svc.merge_branch(world.local, "other"))
    local_status = svc.git_conflict_status(world.local)

    _delegate(world)
    merge = _outcome(lambda: svc.merge_branch(world.pod, "other"))
    status = svc.git_conflict_status(world.pod)

    assert merge == local_merge
    assert status == local_status
    assert status["in_merge"] is True
    assert status["conflicted_files"][0]["regions"]

    with pytest.raises(ValueError, match="conflict markers"):
        svc.git_mark_resolved(world.pod, "a.txt")
    (world.remote / "a.txt").write_text("one\nRESOLVED\n")
    resolved = svc.git_mark_resolved(world.pod, "a.txt")
    assert resolved["all_resolved"] is True
    assert not world.pod.exists()


def test_a_backend_without_the_runner_is_refused(world: SimpleNamespace) -> None:
    class NoRunner:
        owns_credentials = True

    _delegate(world, git=NoRunner())
    with pytest.raises(GitCommandError, match="cannot run cidx's git tools"):
        svc.git_status(world.pod)


def test_no_delegated_git_backend_is_refused(world: SimpleNamespace) -> None:
    world.monkeypatch.setattr(
        backend_wiring, "server_content_backend", lambda: world.content
    )
    with pytest.raises(GitCommandError, match="git_backend='delegated'"):
        svc.git_status(world.pod)


def test_an_unreachable_git_mcp_fails_loud(world: SimpleNamespace) -> None:
    _delegate(world)
    world.git.fail_run = subprocess.CalledProcessError(1, "git-mcp", stderr="down")
    with pytest.raises((GitCommandError, subprocess.CalledProcessError)):
        svc.git_status(world.pod)


def test_a_caller_set_git_variable_cannot_cross_the_seam(
    world: SimpleNamespace,
) -> None:
    from code_indexer.server.services.git_tools_seam import (
        GitToolsUnavailableError,
        delegated_git_tools,
    )

    _delegate(world)
    tools = delegated_git_tools()
    assert tools is not None
    with pytest.raises(GitToolsUnavailableError, match="GIT_DIR"):
        tools.run(["git", "status"], world.pod, env={"GIT_DIR": "/elsewhere"})


def test_local_never_reaches_the_seam(world: SimpleNamespace) -> None:
    svc.git_status(world.local)
    svc.git_fetch(world.local, "origin")
    assert world.git.calls == []


def test_the_local_runner_is_unchanged(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locally the service still calls run_git_command (patched by many tests)."""
    seen: List[List[str]] = []
    real = gos_mod.run_git_command

    def spy(cmd: List[str], **kwargs: Any) -> Any:
        seen.append(list(cmd))
        return real(cmd, **kwargs)

    monkeypatch.setattr(gos_mod, "run_git_command", spy)
    svc.git_status(world.local)
    assert seen and seen[0][:2] == ["git", "status"]


# --------------------------------------------------------------------------
# the MCP git tools' repository resolution
# --------------------------------------------------------------------------


def _registry(world: SimpleNamespace, activated: bool) -> Any:
    return SimpleNamespace(
        user_has_activated_repo=lambda user, alias: activated and user == "alice",
        get_activated_repo_path=lambda username, user_alias: str(world.pod),
    )


def test_mcp_git_resolution_uses_registry_and_seam(world: SimpleNamespace) -> None:
    from code_indexer.server.mcp.handlers import _legacy, _utils

    _delegate(world)
    world.monkeypatch.setattr(
        _utils, "_get_activated_repo_manager", lambda: _registry(world, True)
    )
    assert _legacy._resolve_git_repo_path("repo", "alice") == (str(world.pod), None)
    path, error = _legacy._resolve_git_repo_path("repo", "mallory")
    assert path is None and error is not None and "not found" in error
    assert not world.pod.exists()


def test_local_git_dir_check_never_reaches_the_seam(
    world: SimpleNamespace, tmp_path: Path
) -> None:
    from code_indexer.server.mcp.handlers import _legacy

    assert _legacy._has_git_dir(str(world.local)) is True
    assert _legacy._has_git_dir(str(tmp_path)) is False
    assert world.git.calls == []


def test_delegated_git_dir_check_asks_the_seam(world: SimpleNamespace) -> None:
    from code_indexer.server.mcp.handlers import _legacy

    _delegate(world)
    assert _legacy._has_git_dir(str(world.pod)) is True
    assert ("exec", ["rev-parse", "--git-dir"]) in world.git.calls


def test_mcp_git_resolution_refuses_a_copy_without_git(
    world: SimpleNamespace, tmp_path: Path
) -> None:
    from code_indexer.server.mcp.handlers import _legacy, _utils

    import shutil

    shutil.rmtree(world.remote / ".git")
    _delegate(world)
    world.monkeypatch.setattr(
        _utils, "_get_activated_repo_manager", lambda: _registry(world, True)
    )
    path, error = _legacy._resolve_git_repo_path("repo", "alice")
    assert path is None and error is not None and ".git" in error
