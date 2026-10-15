"""Git tools on a pod that does not hold the repository (content seam, WP3).

With ``content_backend: delegated`` a golden repository lives with
workspace-mcp/git-mcp. The git reads git-mcp exposes run through the git
backend; the rest refuse loudly. These tests pin, for each migrated read:

* parity -- the same answer locally and through the seam (the "git-mcp" here is
  a ``LocalGitBackend`` that maps the pod's path onto a real repository, so the
  pod path itself does not exist and any local git or ``.git`` probe fails);
* delegated -- no local git runs (``run_git_command`` is forbidden);
* fail-loud -- a backend failure is an error, never an empty answer.

And for each refusal: it raises before any git runs, through the seam or
locally. Local behaviour is unchanged: the local path never consults the seam.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

from code_indexer.global_repos import git_operations as golden_git
from code_indexer.global_repos.git_operations import (
    GitOperationsService as GoldenGitService,
    delegated_golden_git_backend,
)
from code_indexer.server.services import git_operations_service as user_git_module
from code_indexer.server.services.delegated_refusal import (
    DelegatedRepositoryUnsupportedError,
)
from code_indexer.server.startup import content_backend_wiring, git_backend_wiring
from code_indexer.server.storage.shared.git_backend import LocalGitBackend

# ── fixtures ───────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, env: Any = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    ).stdout


def _commit(repo: Path, message: str, when: str) -> None:
    import os

    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_DATE=when,
        GIT_COMMITTER_DATE=when,
        GIT_AUTHOR_NAME="Ada",
        GIT_AUTHOR_EMAIL="ada@example.com",
        GIT_COMMITTER_NAME="Ada",
        GIT_COMMITTER_EMAIL="ada@example.com",
    )
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-q", "-m", message, env=env)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with three commits, a multi-line message, non-ASCII content,
    a remote with a tracking branch, and a remote-only branch."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "a.txt").write_text("one\n")
    _commit(origin, "first", "2024-01-02T03:04:05+0000")
    (origin / "a.txt").write_text("two – ünïcode\r\nline\n")
    _commit(origin, "second\n\nbody line", "2024-02-03T04:05:06+0200")
    _git(origin, "branch", "remote-only")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    (clone / "b.txt").write_text("b\n")
    _commit(clone, 'third: subject with "quotes"', "2024-03-04T05:06:07-0500")
    return clone


class MappedGit:
    """git-mcp stand-in: a LocalGitBackend that sees *pod_path* at *real*.

    Records every call. The pod path does not exist, so only the seam can
    answer for it.
    """

    def __init__(self, pod_path: Path, real: Path) -> None:
        self._pod, self._real = str(pod_path), str(real)
        self._local = LocalGitBackend()
        self.calls: List[Tuple[str, Tuple[Any, ...]]] = []
        self.fail: Exception | None = None

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._local, name)

        def call(repo_path: str, *args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args))
            if self.fail is not None:
                raise self.fail
            assert repo_path == self._pod, repo_path
            return method(self._real, *args, **kwargs)

        return call


@pytest.fixture
def pod(tmp_path: Path) -> Path:
    path = tmp_path / "pod" / "golden-repos" / "repo"
    assert not path.exists()
    return path


@pytest.fixture
def seam(repo: Path, pod: Path) -> MappedGit:
    return MappedGit(pod, repo)


@pytest.fixture
def delegated(monkeypatch: pytest.MonkeyPatch, seam: MappedGit) -> MappedGit:
    """The running server reads repositories through a delegated seam."""
    monkeypatch.setattr(
        content_backend_wiring,
        "server_content_backend",
        lambda: SimpleNamespace(is_local=False),
    )
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: seam
    )
    return seam


def _forbid_local_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any local git from the services under test fails the test."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"local git ran: {args[:1]}")

    monkeypatch.setattr(golden_git, "run_git_command", forbidden)
    monkeypatch.setattr(user_git_module, "run_git_command", forbidden)


@pytest.fixture
def no_local_git(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_local_git(monkeypatch)


# ── golden history service ─────────────────────────────────────────────────


def test_golden_reads_match_local_through_the_seam(
    repo: Path, pod: Path, seam: MappedGit, monkeypatch
) -> None:
    head_sha = _git(repo, "rev-parse", "HEAD").strip()
    local = GoldenGitService(repo)
    expected = (
        local.get_log(limit=2),
        local.get_log(limit=5, since="2024-01-15", branch="HEAD~1"),
        local.show_commit(head_sha[:8], include_stats=False),
        local.show_commit("HEAD~2", include_stats=False),
        local.get_file_at_revision("a.txt", "HEAD~1"),
    )
    _forbid_local_git(monkeypatch)
    remote = GoldenGitService(pod, git_backend=seam)
    actual = (
        remote.get_log(limit=2),
        remote.get_log(limit=5, since="2024-01-15", branch="HEAD~1"),
        remote.show_commit(head_sha[:8], include_stats=False),
        remote.show_commit("HEAD~2", include_stats=False),
        remote.get_file_at_revision("a.txt", "HEAD~1"),
    )
    assert actual == expected
    assert "\n" in expected[4].content and "ü" in expected[4].content


@pytest.mark.parametrize(
    "call, message",
    [
        (lambda s: s.get_file_at_revision("a.txt", "no-such-rev"), "Invalid revision"),
        (lambda s: s.get_file_at_revision("missing.txt", "HEAD"), "File not found"),
        (lambda s: s.show_commit("no-such-rev", include_stats=False), "not found"),
    ],
)
def test_golden_errors_match_local(
    repo: Path, pod: Path, seam: MappedGit, call: Any, message: str
) -> None:
    with pytest.raises(ValueError, match=message) as local:
        call(GoldenGitService(repo))
    with pytest.raises(ValueError, match=message) as remote:
        call(GoldenGitService(pod, git_backend=seam))
    assert str(remote.value) == str(local.value)


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_blame("a.txt"),
        lambda s: s.get_file_history("a.txt"),
        lambda s: s.search_commits("first"),
        lambda s: s.search_diffs(search_string="one"),
        lambda s: s.get_diff("HEAD~1", "HEAD"),
        lambda s: s.get_log(path="a.txt"),
        lambda s: s.get_log(author="Ada"),
        lambda s: s.show_commit("HEAD"),  # include_stats defaults to True
        lambda s: s.show_commit("HEAD", include_stats=False, include_diff=True),
    ],
)
def test_golden_without_a_seam_operation_refuses_before_any_git(
    pod: Path, seam: MappedGit, no_local_git: None, call: Any
) -> None:
    with pytest.raises(DelegatedRepositoryUnsupportedError, match="git-mcp exposes"):
        call(GoldenGitService(pod, git_backend=seam))
    assert seam.calls == []


def test_golden_backend_failure_is_an_error_not_empty(
    pod: Path, seam: MappedGit
) -> None:
    seam.fail = ConnectionError("git-mcp unreachable")
    service = GoldenGitService(pod, git_backend=seam)
    with pytest.raises(ConnectionError):
        service.get_log()
    with pytest.raises(ConnectionError):
        service.get_file_at_revision("a.txt", "HEAD")


def test_golden_local_service_still_requires_a_local_git_dir(pod: Path) -> None:
    with pytest.raises(ValueError, match="Not a git repository"):
        GoldenGitService(pod)


def test_delegated_golden_git_backend_selection(
    monkeypatch: pytest.MonkeyPatch, seam: MappedGit
) -> None:
    monkeypatch.setattr(content_backend_wiring, "server_content_backend", lambda: None)
    assert delegated_golden_git_backend() is None
    monkeypatch.setattr(
        content_backend_wiring,
        "server_content_backend",
        lambda: SimpleNamespace(is_local=False),
    )
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: seam
    )
    assert delegated_golden_git_backend() is seam
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: None
    )
    with pytest.raises(DelegatedRepositoryUnsupportedError, match="git_backend"):
        delegated_golden_git_backend()


# ── MCP path resolution and handlers ───────────────────────────────────────


@pytest.fixture
def legacy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pod: Path) -> Any:
    from code_indexer.server.mcp.handlers import _legacy

    monkeypatch.setattr(_legacy, "_get_golden_repos_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        _legacy, "_get_global_repo", lambda alias: {"repo_url": "git@x:r.git"}
    )
    monkeypatch.setattr(_legacy, "_resolve_repo_path", lambda alias, d: str(pod))
    monkeypatch.setattr(
        _legacy,
        "_get_access_filtering_service",
        lambda: SimpleNamespace(get_accessible_repos=lambda user: {"repo"}),
    )
    return _legacy


def test_resolve_git_repo_path_delegated(
    legacy: Any, delegated: MappedGit, pod: Path
) -> None:
    path, error = legacy._resolve_git_repo_path(
        "repo-global", "u", via_git_backend=True
    )
    assert (path, error) == (str(pod), None)

    path, error = legacy._resolve_git_repo_path("repo-global", "u")
    assert path is None and "not supported with content_backend='delegated'" in error

    # An invisible repository stays invisible: access is checked first.
    path, error = legacy._resolve_git_repo_path("other-global", "u")
    assert (path, error) == (None, "Repository 'other-global' not found.")


def test_resolve_git_repo_path_local_unchanged(legacy: Any, monkeypatch) -> None:
    monkeypatch.setattr(content_backend_wiring, "server_content_backend", lambda: None)
    for kwargs in ({}, {"via_git_backend": True}):
        path, error = legacy._resolve_git_repo_path("repo-global", "u", **kwargs)
        assert path is None and "is a local repository" in error


def test_resolve_repo_path_skips_git_probes_when_delegated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, delegated: MappedGit
) -> None:
    from code_indexer.server.mcp.handlers import _legacy

    index_dir = tmp_path / "golden-repos" / "repo"
    index_dir.mkdir(parents=True)
    monkeypatch.setattr(_legacy.AliasManager, "existing", staticmethod(lambda p: None))
    monkeypatch.setattr(
        _legacy, "_get_global_repo", lambda alias: {"index_path": str(index_dir)}
    )

    def probe(path: Path) -> bool:
        raise AssertionError(f"probed {path} for .git on a delegated pod")

    monkeypatch.setattr(_legacy, "_is_git_repo", probe)
    assert _legacy._resolve_repo_path("repo-global", str(tmp_path)) == str(index_dir)


def _payload(response: Any) -> Any:
    import json

    return json.loads(response["content"][0]["text"])


@pytest.fixture
def handlers(legacy: Any, delegated: MappedGit, monkeypatch, pod: Path) -> Any:
    from code_indexer.server.mcp.handlers import git_read

    def resolve(alias: str, user: str, via_git_backend: bool = False) -> Any:
        assert via_git_backend, "a seam-aware tool must ask for the seam path"
        return str(pod), None

    monkeypatch.setattr(legacy, "_resolve_git_repo_path", resolve)
    return git_read


def test_handler_reads_through_the_seam(handlers: Any, no_local_git: None) -> None:
    user = SimpleNamespace(username="u")
    data = _payload(
        handlers.handle_git_file_at_revision(
            {"repository_alias": "repo-global", "path": "a.txt", "revision": "HEAD~2"},
            user,
        )
    )
    assert data["success"] is True and data["content"] == "one\n"


def test_handler_refusal_is_an_error(handlers: Any, no_local_git: None) -> None:
    user = SimpleNamespace(username="u")
    data = _payload(
        handlers.handle_git_blame(
            {"repository_alias": "repo-global", "path": "a.txt"}, user
        )
    )
    assert data["success"] is False
    assert (
        "git_blame is not supported with content_backend='delegated'" in data["error"]
    )


# ── user git tools on golden repos ─────────────────────────────────────────


@pytest.fixture
def user_service() -> Any:
    service = user_git_module.GitOperationsService()
    from code_indexer.server.utils.config_manager import (
        ApiLimitsConfig,
        GitTimeoutsConfig,
    )

    service._git_timeouts = GitTimeoutsConfig(git_local_timeout=30)
    service._api_limits = ApiLimitsConfig(max_log_commits=500)
    return service


def test_user_git_reads_match_local_through_the_seam(
    repo: Path, pod: Path, seam: MappedGit, user_service: Any, monkeypatch
) -> None:
    (repo / "a.txt").write_text("dirty\n")
    (repo / "new.txt").write_text("new\n")
    expected = (
        user_service.git_log(repo, limit=2, offset=1),
        user_service.git_log(repo, branch="origin/main"),
        user_service.git_branch_list(repo),
        user_service.git_status(repo),
    )
    _forbid_local_git(monkeypatch)
    actual = (
        user_service.git_log(pod, limit=2, offset=1, git_backend=seam),
        user_service.git_log(pod, branch="origin/main", git_backend=seam),
        user_service.git_branch_list(pod, git_backend=seam),
        user_service.git_status(pod, git_backend=seam),
    )
    assert actual == expected
    assert expected[3]["untracked"] == ["new.txt"]


def test_user_git_log_with_filters_refuses(
    pod: Path, seam: MappedGit, user_service: Any
) -> None:
    for filters in ({"since_date": "2024-01-01"}, {"author": "Ada"}, {"path": "a"}):
        with pytest.raises(DelegatedRepositoryUnsupportedError):
            user_service.git_log(pod, git_backend=seam, **filters)
    assert seam.calls == []


def test_user_git_backend_failure_is_an_error(
    pod: Path, seam: MappedGit, user_service: Any
) -> None:
    seam.fail = subprocess.CalledProcessError(128, ["git"], stderr="boom")
    with pytest.raises(user_git_module.GitCommandError):
        user_service.git_log(pod, git_backend=seam)
    with pytest.raises(user_git_module.GitCommandError):
        user_service.git_status(pod, git_backend=seam)


# ── repo metrics, index freshness, branch listing ──────────────────────────


def test_repo_metrics_through_the_seam(
    repo: Path, pod: Path, tmp_path: Path, monkeypatch, caplog
) -> None:
    from code_indexer.services import progress_subprocess_runner as runner

    expected = runner.gather_repo_metrics(repo)
    assert expected == (2, 3)
    seam = MappedGit(pod, repo)
    monkeypatch.setattr(
        content_backend_wiring,
        "server_content_backend",
        lambda: SimpleNamespace(is_local=False),
    )
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: seam
    )

    # The pod path has no .git (it does not exist): only the seam can count.
    assert runner.gather_repo_metrics(pod) == expected

    # A local:// folder (not a repository) counts nothing, quietly.
    plain = tmp_path / "plain"
    plain.mkdir()
    not_repo = MappedGit(pod, plain)
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: not_repo
    )
    with caplog.at_level(logging.WARNING):
        assert runner.gather_repo_metrics(pod) == (0, 0)
    assert "git backend failed" not in caplog.text

    # A seam failure is logged, never silent.
    seam.fail = ConnectionError("git-mcp unreachable")
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: seam
    )
    with caplog.at_level(logging.WARNING):
        assert runner.gather_repo_metrics(pod) == (0, 0)
    assert "git-mcp unreachable" in caplog.text


def test_index_freshness_commit_through_the_seam(
    repo: Path, pod: Path, monkeypatch, caplog
) -> None:
    from code_indexer.server.services import index_freshness

    expected = index_freshness.current_commit(repo)
    assert expected and len(expected) == 40
    seam = MappedGit(pod, repo)
    monkeypatch.setattr(
        content_backend_wiring,
        "server_content_backend",
        lambda: SimpleNamespace(is_local=False),
    )
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: seam
    )
    assert index_freshness.current_commit(pod) == expected
    assert seam.calls == [("rev_parse", ("HEAD",))]

    seam.fail = ConnectionError("git-mcp unreachable")
    with caplog.at_level(logging.WARNING):
        assert index_freshness.current_commit(pod) is None
    assert "git-mcp unreachable" in caplog.text

    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: None
    )
    with caplog.at_level(logging.WARNING):
        assert index_freshness.current_commit(pod) is None
    assert "git_backend is local" in caplog.text


def test_branch_listing_through_the_seam(repo: Path, pod: Path, monkeypatch) -> None:
    from code_indexer.server.services import branch_service
    from code_indexer.services.git_topology_service import GitTopologyService

    def listing(path: Path) -> Any:
        branch_service._branches_cache.clear()
        with branch_service.BranchService(GitTopologyService(path)) as service:
            return (
                service.list_branches(include_remote=True),
                service.get_branch_by_name("main"),
            )

    expected = listing(repo)
    assert {b.name for b in expected[0]} == {"main", "remote-only"}
    seam = MappedGit(pod, repo)
    monkeypatch.setattr(
        content_backend_wiring,
        "server_content_backend",
        lambda: SimpleNamespace(is_local=False),
    )
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: seam
    )
    assert listing(pod) == expected
    # ONE git-mcp listing per call whatever the branch count (list_branches
    # with a custom --format is not something git-mcp can answer), plus the
    # ahead/behind counts of a tracking branch and the topology probe.
    names = [name for name, _ in seam.calls]
    assert names.count("for_each_ref") == 2
    assert set(names) <= {"for_each_ref", "rev_list_count", "rev_parse"}

    seam.fail = ConnectionError("git-mcp unreachable")
    branch_service._branches_cache.clear()
    with branch_service.BranchService(GitTopologyService(pod)) as service:
        with pytest.raises(ConnectionError):
            service.list_branches()

    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: None
    )
    with pytest.raises(DelegatedRepositoryUnsupportedError):
        branch_service.BranchService(GitTopologyService(pod))


# ── no production caller ───────────────────────────────────────────────────


_SRC = Path(__file__).resolve().parents[3] / "src" / "code_indexer"


def _src_mentions(token: str) -> List[str]:
    return sorted(
        str(p.relative_to(_SRC))
        for p in _SRC.rglob("*.py")
        if token in p.read_text(encoding="utf-8")
    )


def test_git_sync_executor_has_no_production_caller() -> None:
    """GitSyncExecutor (and the GitStateManager.clear_repo_before_refresh,
    get_current_branch/is_git_repository calls inside it) is only re-exported
    by its package: nothing in the server constructs it."""
    assert _src_mentions("GitSyncExecutor(") == []
    assert _src_mentions("GitSyncExecutor") == [
        "server/git/__init__.py",
        "server/git/git_sync_executor.py",
    ]


def test_git_state_manager_has_no_production_caller() -> None:
    """GitStateManager is built only by GitSyncExecutor and the SCIP
    self-healing service, which nothing in the server constructs."""
    assert _src_mentions("GitStateManager(") == [
        "server/git/git_sync_executor.py",
        "server/services/scip_self_healing.py",
    ]
    assert _src_mentions("SCIPSelfHealingService(") == []


class GitMcpEmulator:
    """git-mcp's JSON-RPC tools for one repository, seen at *pod*.

    Tool results follow git-mcp: ``git_symbolic_ref`` -> ``{"ref"}``,
    ``git_rev_parse`` -> ``{"output"}``, ``git_branch_list`` -> simple-git's
    branch summary (``remotes/`` stripped, ``type``, ``current``; the
    symbolic ``origin/HEAD`` line is not a branch to it), the raw tools ->
    ``{"exit_code", "stdout_b64", "stderr"}``. A git failure in a
    structured tool is a tool error whose text is git's stderr.
    """

    def __init__(self, pod: Path, real: Path) -> None:
        self._pod, self._real = str(pod), real
        self.down = False
        self.tools: List[str] = []

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self._real, capture_output=True)

    def _structured(self, args: List[str], key: str) -> Any:
        out = self._git(*args)
        if out.returncode:
            raise _ToolError(out.stderr.decode())
        return {key: out.stdout.decode().strip()}

    def _raw(self, args: List[str]) -> Any:
        import base64

        out = self._git(*args)
        return {
            "exit_code": out.returncode,
            "stdout_b64": base64.b64encode(out.stdout).decode(),
            "stderr": out.stderr.decode(),
        }

    def _branch_list(self, include_remote: bool) -> Any:
        listed = self._git(
            "for-each-ref", "--format=%(HEAD) %(refname) %(symref)", "refs/heads"
        ).stdout.decode()
        if include_remote:
            listed += self._git(
                "for-each-ref", "--format=%(HEAD) %(refname) %(symref)", "refs/remotes"
            ).stdout.decode()
        branches = []
        for line in listed.splitlines():
            head = line[0]
            refname, _, symref = line[2:].partition(" ")
            if symref.strip():
                continue
            remote = refname.startswith("refs/remotes/")
            branches.append(
                {
                    "name": refname.split("/", 2)[2],
                    "type": "remote" if remote else "local",
                    "current": head == "*",
                }
            )
        return {"total": len(branches), "branches": branches}

    def tool(self, name: str, a: Any) -> Any:
        self.tools.append(name)
        assert a["cow_path"] == self._pod, a
        if name == "git_symbolic_ref":
            short = ["--short"] if a.get("short") else []
            return self._structured(["symbolic-ref", *short, "HEAD"], "ref")
        if name == "git_rev_parse":
            return self._structured(["rev-parse", *a["args"]], "output")
        if name == "git_branch_list":
            return self._branch_list(a.get("include_remote", True))
        if name == "git_for_each_ref":
            return self._raw(
                [
                    "for-each-ref",
                    f"--format={a['format']}",
                    "--",
                    *a.get("patterns", []),
                ]
            )
        if name == "git_log_raw":
            args = ["log", f"--format={a['format']}"]
            if a.get("since"):
                args.append(f"--since={a['since']}")
            if a.get("max_count"):
                args.append(f"--max-count={a['max_count']}")
            return self._raw([*args, *a.get("revs", [])])
        if name == "git_show_objects":
            items = [self._raw(["show", f"{s['rev']}:{s['path']}"]) for s in a["specs"]]
            return {"objects": items}
        raise AssertionError(f"tool not emulated: {name}")

    def urlopen(self, req: Any, timeout: Any = None) -> Any:
        import io
        import json
        import urllib.error

        if self.down:
            raise urllib.error.URLError("[Errno 111] Connection refused")
        params = json.loads(req.data.decode())["params"]
        result: Dict[str, Any]
        try:
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            self.tool(params["name"], params["arguments"])
                        ),
                    }
                ]
            }
        except _ToolError as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return io.BytesIO(json.dumps({"jsonrpc": "2.0", "result": result}).encode())


class _ParentsFail(MappedGit):
    """git-mcp failing only the parent read of a commit it just resolved."""

    def rev_parse(self, repo_path: str, *args: str, **kwargs: Any) -> str:
        if args and str(args[-1]).endswith("^@"):
            raise subprocess.CalledProcessError(1, "git-mcp", stderr="git-mcp down")
        return str(self.__getattr__("rev_parse")(repo_path, *args, **kwargs))


class _ToolError(Exception):
    pass


@pytest.fixture


class _TruncatedRefs(MappedGit):
    """git-mcp answering for-each-ref with a record cut short."""

    def for_each_ref(self, repo_path: str, *args: Any, **kwargs: Any) -> bytes:
        return b"*\x00refs/heads/main\n"


    def _branch_list(self, include_remote: bool) -> Any:
        listed = self._git(
            "for-each-ref", "--format=%(HEAD) %(refname) %(symref)", "refs/heads"
        ).stdout.decode()
        if include_remote:
            listed += self._git(
                "for-each-ref", "--format=%(HEAD) %(refname) %(symref)", "refs/remotes"
            ).stdout.decode()
        branches = []
        for line in listed.splitlines():
            head = line[0]
            refname, _, symref = line[2:].partition(" ")
            if symref.strip():
                continue
            remote = refname.startswith("refs/remotes/")
            branches.append(
                {
                    "name": refname.split("/", 2)[2],
                    "type": "remote" if remote else "local",
                    "current": head == "*",
                }
            )
        return {"total": len(branches), "branches": branches}


def _load_git_mcp_plugin() -> Any:
    import importlib.util

    source = (
        Path(__file__).resolve().parents[3]
        / "plugins"
        / "cidx_git_backend"
        / "cidx_git_backend"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("cidx_git_backend_contract", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin_backend(
    plugin: Any, pod: Path, real: Path, monkeypatch: pytest.MonkeyPatch
) -> Tuple[Any, GitMcpEmulator]:
    emulator = GitMcpEmulator(pod, real)
    monkeypatch.setattr(plugin.urllib.request, "urlopen", emulator.urlopen)
    return plugin.GitMcpBackend(base_url="http://git-mcp:3000"), emulator


    def _raw(self, args: List[str]) -> Any:
        import base64

        out = self._git(*args)
        return {
            "exit_code": out.returncode,
            "stdout_b64": base64.b64encode(out.stdout).decode(),
            "stderr": out.stderr.decode(),
        }


    def _structured(self, args: List[str], key: str) -> Any:
        out = self._git(*args)
        if out.returncode:
            raise _ToolError(out.stderr.decode())
        return {key: out.stdout.decode().strip()}


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    path = tmp_path / "empty"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    return path


    def for_each_ref(self, repo_path: str, *args: Any, **kwargs: Any) -> bytes:
        return b"*\x00refs/heads/main\n"


@pytest.fixture
def git_mcp_plugin() -> Any:
    return _load_git_mcp_plugin()


    def rev_parse(self, repo_path: str, *args: str, **kwargs: Any) -> str:
        if args and str(args[-1]).endswith("^@"):
            raise subprocess.CalledProcessError(1, "git-mcp", stderr="git-mcp down")
        return str(self.__getattr__("rev_parse")(repo_path, *args, **kwargs))


def test_branch_listing_of_a_pod_local_repo_stays_local(
    tmp_path: Path, monkeypatch
) -> None:
    """A local:// repository (Langfuse traces) is on this pod: not a git
    repository is the answer, with or without git-mcp -- never a refusal."""
    from code_indexer.server.services import branch_service
    from code_indexer.services.git_topology_service import GitTopologyService

    folder = tmp_path / "golden-repos" / "langfuse_traces"
    folder.mkdir(parents=True)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(
        content_backend_wiring,
        "server_content_backend",
        lambda: SimpleNamespace(is_local=False),
    )
    monkeypatch.setattr(
        content_backend_wiring,
        "_golden_repo_record",
        lambda alias: SimpleNamespace(
            repo_url=f"local://{alias}", clone_path=str(folder)
        ),
    )
    for backend in (None, MappedGit(folder, folder)):
        monkeypatch.setattr(
            git_backend_wiring, "server_delegated_git_backend", lambda: backend
        )
        branch_service._branches_cache.clear()
        with branch_service.BranchService(GitTopologyService(folder)) as service:
            assert service.list_branches(include_remote=True) == []
        if backend is not None:
            assert backend.calls == []


# ── no production caller ───────────────────────────────────────────────────


def test_golden_empty_repository_through_the_real_git_mcp_plugin(
    empty_repo: Path, pod: Path, git_mcp_plugin: Any, monkeypatch
) -> None:
    expected = GoldenGitService(empty_repo).get_log()
    backend, _ = _plugin_backend(git_mcp_plugin, pod, empty_repo, monkeypatch)
    assert GoldenGitService(pod, git_backend=backend).get_log() == expected
    assert expected.commits == []


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_log(),
        lambda s: s.show_commit("HEAD", include_stats=False),
        lambda s: s.get_file_at_revision("a.txt", "HEAD"),
    ],
    ids=["get_log", "show_commit", "get_file_at_revision"],
)


def test_golden_git_mcp_outage_is_an_error_not_an_empty_answer(
    repo: Path, pod: Path, git_mcp_plugin: Any, monkeypatch, call: Any
) -> None:
    backend, emulator = _plugin_backend(git_mcp_plugin, pod, repo, monkeypatch)
    emulator.down = True
    with pytest.raises(subprocess.CalledProcessError, match="Connection refused"):
        call(GoldenGitService(pod, git_backend=backend))


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_log(),
        lambda s: s.show_commit("HEAD", include_stats=False),
        lambda s: s.get_file_at_revision("a.txt", "HEAD"),
    ],
    ids=["get_log", "show_commit", "get_file_at_revision"],
)


def test_golden_missing_clone_is_an_error_not_an_empty_answer(
    tmp_path: Path, pod: Path, git_mcp_plugin: Any, monkeypatch, call: Any
) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    backend, _ = _plugin_backend(git_mcp_plugin, pod, not_a_repo, monkeypatch)
    with pytest.raises(subprocess.CalledProcessError, match="not a git repository"):
        call(GoldenGitService(pod, git_backend=backend))


def test_golden_reads_through_the_real_git_mcp_plugin(
    repo: Path, pod: Path, git_mcp_plugin: Any, monkeypatch
) -> None:
    local = GoldenGitService(repo)
    expected = (
        local.get_log(limit=2),
        local.get_log(branch="no-such-branch"),
        local.show_commit("HEAD~1", include_stats=False),
        local.get_file_at_revision("a.txt", "HEAD~1"),
    )
    backend, _ = _plugin_backend(git_mcp_plugin, pod, repo, monkeypatch)
    _forbid_local_git(monkeypatch)
    remote = GoldenGitService(pod, git_backend=backend)
    assert (
        remote.get_log(limit=2),
        remote.get_log(branch="no-such-branch"),
        remote.show_commit("HEAD~1", include_stats=False),
        remote.get_file_at_revision("a.txt", "HEAD~1"),
    ) == expected
    assert expected[1].commits == []
    for call, message in (
        (lambda s: s.show_commit("no-such-rev", include_stats=False), "not found"),
        (lambda s: s.get_file_at_revision("a.txt", "no-such-rev"), "Invalid revision"),
    ):
        with pytest.raises(ValueError, match=message):
            call(remote)


def test_golden_show_commit_parent_failure_is_an_error(pod: Path, repo: Path) -> None:
    service = GoldenGitService(pod, git_backend=_ParentsFail(pod, repo))
    with pytest.raises(subprocess.CalledProcessError) as raised:
        service.show_commit("HEAD", include_stats=False)
    assert raised.value.stderr == "git-mcp down"


def test_user_branch_list_malformed_seam_output_is_an_error(
    pod: Path, repo: Path, user_service: Any
) -> None:
    with pytest.raises(
        user_git_module.GitCommandError, match="unexpected for-each-ref"
    ):
        user_service.git_branch_list(pod, git_backend=_TruncatedRefs(pod, repo))


def test_user_branch_list_on_a_detached_head_through_the_seam(
    repo: Path,
    pod: Path,
    seam: MappedGit,
    user_service: Any,
    git_mcp_plugin: Any,
    monkeypatch,
) -> None:
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    expected = user_service.git_branch_list(repo)
    assert expected["current"].startswith("(HEAD detached at ")
    backend, _ = _plugin_backend(git_mcp_plugin, pod, repo, monkeypatch)
    _forbid_local_git(monkeypatch)

    assert user_service.git_branch_list(pod, git_backend=seam) == expected
    assert user_service.git_branch_list(pod, git_backend=backend) == expected


def test_user_branch_list_seam_failure_is_an_error(
    repo: Path, pod: Path, user_service: Any, git_mcp_plugin: Any, monkeypatch
) -> None:
    backend, emulator = _plugin_backend(git_mcp_plugin, pod, repo, monkeypatch)
    emulator.down = True
    with pytest.raises(user_git_module.GitCommandError, match="Connection refused"):
        user_service.git_branch_list(pod, git_backend=backend)


@pytest.fixture


def test_user_branch_list_through_the_real_git_mcp_plugin(
    repo: Path, pod: Path, user_service: Any, git_mcp_plugin: Any, monkeypatch
) -> None:
    expected = user_service.git_branch_list(repo)
    assert expected["current"] == "main"
    assert "origin/remote-only" in expected["remote"]
    backend, emulator = _plugin_backend(git_mcp_plugin, pod, repo, monkeypatch)
    _forbid_local_git(monkeypatch)

    assert user_service.git_branch_list(pod, git_backend=backend) == expected
    assert "git_branch_list" not in emulator.tools


    def tool(self, name: str, a: Any) -> Any:
        self.tools.append(name)
        assert a["cow_path"] == self._pod, a
        if name == "git_symbolic_ref":
            short = ["--short"] if a.get("short") else []
            return self._structured(["symbolic-ref", *short, "HEAD"], "ref")
        if name == "git_rev_parse":
            return self._structured(["rev-parse", *a["args"]], "output")
        if name == "git_branch_list":
            return self._branch_list(a.get("include_remote", True))
        if name == "git_for_each_ref":
            return self._raw(
                [
                    "for-each-ref",
                    f"--format={a['format']}",
                    "--",
                    *a.get("patterns", []),
                ]
            )
        if name == "git_log_raw":
            args = ["log", f"--format={a['format']}"]
            if a.get("since"):
                args.append(f"--since={a['since']}")
            if a.get("max_count"):
                args.append(f"--max-count={a['max_count']}")
            return self._raw([*args, *a.get("revs", [])])
        if name == "git_show_objects":
            items = [self._raw(["show", f"{s['rev']}:{s['path']}"]) for s in a["specs"]]
            return {"objects": items}
        raise AssertionError(f"tool not emulated: {name}")


    def urlopen(self, req: Any, timeout: Any = None) -> Any:
        import io
        import json
        import urllib.error

        if self.down:
            raise urllib.error.URLError("[Errno 111] Connection refused")
        params = json.loads(req.data.decode())["params"]
        result: Dict[str, Any]
        try:
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            self.tool(params["name"], params["arguments"])
                        ),
                    }
                ]
            }
        except _ToolError as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return io.BytesIO(json.dumps({"jsonrpc": "2.0", "result": result}).encode())
