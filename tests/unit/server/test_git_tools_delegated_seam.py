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
