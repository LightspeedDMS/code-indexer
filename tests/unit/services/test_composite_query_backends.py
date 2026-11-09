"""Composite ``cidx query`` on a pod whose member directory holds index state
only (issue-64616).

The server spawns ``cidx query`` per composite member. With the repositories
delegated the member directory on the pod holds ``.code-indexer`` only, so the
branch filter (``GenericQueryService``) asks git-mcp (``--git-backend``), and
with ``--content-backend=delegated`` staleness is skipped rather than guessed.
Pinned here:

* parity: the delegated branch context and branch filter equal the local ones
  over the same repository, with every local access to the pod's
  member directory -- and any subprocess run in it -- raising;
* the fake git-mcp enforces git_exec's argv policy for what the filter sends;
* fail loud: git-mcp unreachable raises, never an unfiltered result;
* the server stamps the flags only when delegated (local command unchanged).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

from code_indexer import cli as cli_module
from code_indexer.server.storage.shared.git_backend import local_git_backend
from code_indexer.services.generic_query_service import (
    BranchContextUnavailableError,
    GenericQueryService,
)
from tests.unit.server.activated_seam_fakes import (
    LocalIOForbidden,
    install_local_io_guards,
)

#: git_exec's allowlist (git-mcp git-exec.policy.ts) for what the filter sends.
_POLICY = {
    "rev-parse": {"--is-inside-work-tree", "--abbrev-ref"},
    "ls-tree": {"-r", "--name-only"},
    "merge-base": {"--is-ancestor"},
}


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="Example",
        GIT_AUTHOR_EMAIL="dev@example.com",
        GIT_COMMITTER_NAME="Example",
        GIT_COMMITTER_EMAIL="dev@example.com",
    )
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


class FakeGitMcp:
    """git-mcp ``git_exec`` over the remote tree, addressed by the POD path."""

    def __init__(self, pod: Path, remote: Path) -> None:
        self._pod, self._remote = pod, remote
        self._inner = local_git_backend()
        self.calls: List[List[str]] = []
        self.unreachable = False

    def run(
        self, repo_path: str, args: Sequence[str], timeout: Any = None
    ) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if self.unreachable:
            raise subprocess.CalledProcessError(1, ["git-mcp"], stderr="reset")
        allowed = _POLICY.get(args[0])
        if allowed is None:
            raise RuntimeError(f"git_exec does not run {args[0]!r}")
        for arg in args[1:]:
            if arg.startswith("-") and arg not in allowed:
                raise RuntimeError(f"git_exec refuses option {arg} for {args[0]}")
        mapped = self._remote / Path(repo_path).relative_to(self._pod)
        return self._inner.run(str(mapped), list(args), timeout=timeout)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.local = tmp_path / "local" / "member"
        self.local.mkdir(parents=True)
        _git(self.local, "init", "-q", "-b", "main")
        (self.local / "a.py").write_text("print('a')\n")
        _git(self.local, "add", ".")
        _git(self.local, "commit", "-q", "-m", "one")
        self.first = _git(self.local, "rev-parse", "HEAD")
        (self.local / "b.py").write_text("print('b')\n")
        _git(self.local, "add", ".")
        _git(self.local, "commit", "-q", "-m", "two")
        self.pod_root = tmp_path / "pod"
        self.remote_root = tmp_path / "remote"
        self.pod = self.pod_root / "member"
        shutil.copytree(self.local, self.remote_root / "member", symlinks=True)
        (self.pod / ".code-indexer").mkdir(parents=True)  # index state only
        self.git_mcp = FakeGitMcp(self.pod_root, self.remote_root)

    def forbid(self, patcher: pytest.MonkeyPatch) -> None:
        root = str(self.pod)

        def forbidden(value: Any) -> bool:
            try:
                full = os.path.abspath(os.fspath(value))
            except TypeError:
                return False
            if full != root and not full.startswith(root + os.sep):
                return False
            rel = Path(full).relative_to(root).parts
            return not rel or rel[0] != ".code-indexer"

        install_local_io_guards(patcher, forbidden)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


RESULTS: List[Dict[str, Any]] = [
    {"payload": {"path": "a.py", "git_available": True}},
    {"payload": {"path": "b.py", "git_available": True}},
    {"payload": {"path": "gone.py", "git_available": True}},
    {"payload": {"path": "old.py", "git_available": True, "git_commit_hash": ""}},
    {"payload": {"path": "plain.txt", "git_available": False}},
]


def _reachable_from(world: World) -> List[Dict[str, Any]]:
    return [
        {"payload": {"path": "x.py", "git_available": True, "git_commit_hash": h}}
        for h in (world.first, "0" * 40)
    ]


def test_the_guard_is_live(world: World) -> None:
    with pytest.MonkeyPatch.context() as patcher:
        world.forbid(patcher)
        with pytest.raises(LocalIOForbidden):
            (world.pod / "a.py").exists()
        with pytest.raises(LocalIOForbidden):
            subprocess.run(["git", "status"], cwd=world.pod)


def test_branch_context_through_git_mcp_equals_local(world: World) -> None:
    local = GenericQueryService(world.local)._get_current_branch_context()
    service = GenericQueryService(world.pod, git_backend=world.git_mcp)
    with pytest.MonkeyPatch.context() as patcher:
        world.forbid(patcher)
        delegated = service._get_current_branch_context()
    assert delegated == local
    assert delegated["files"] == {"a.py", "b.py"}
    assert delegated["branch"] == "main"


def test_branch_filter_through_git_mcp_equals_local(world: World) -> None:
    rows = RESULTS + _reachable_from(world)
    local = GenericQueryService(world.local).filter_results_by_branch(rows)
    service = GenericQueryService(world.pod, git_backend=world.git_mcp)
    with pytest.MonkeyPatch.context() as patcher:
        world.forbid(patcher)
        delegated = service.filter_results_by_branch(rows)
    assert delegated == local
    kept = [r["payload"]["path"] for r in delegated]
    assert kept == ["a.py", "b.py", "plain.txt", "x.py"]


def test_reachability_through_git_mcp_equals_local(world: World) -> None:
    local = GenericQueryService(world.local)
    delegated = GenericQueryService(world.pod, git_backend=world.git_mcp)
    for commit in (world.first, "0" * 40):
        assert delegated._is_commit_reachable(commit) is local._is_commit_reachable(
            commit
        )
    assert ["merge-base", "--is-ancestor", world.first, "HEAD"] in world.git_mcp.calls


def test_an_unreachable_git_mcp_fails_the_filter_loudly(world: World) -> None:
    world.git_mcp.unreachable = True
    service = GenericQueryService(world.pod, git_backend=world.git_mcp)
    with pytest.raises(BranchContextUnavailableError, match="git-mcp"):
        service.filter_results_by_branch(RESULTS)


def test_a_failed_reachability_check_is_not_an_included_result(
    world: World,
) -> None:
    service = GenericQueryService(world.pod, git_backend=world.git_mcp)
    context = service._get_current_branch_context()
    world.git_mcp.unreachable = True
    row = _reachable_from(world)[0]
    with pytest.raises(BranchContextUnavailableError):
        service._is_result_current_branch(row, context)


def test_a_backend_that_cannot_run_git_is_refused(world: World) -> None:
    service = GenericQueryService(world.pod, git_backend=object())
    with pytest.raises(BranchContextUnavailableError, match="cannot run git"):
        service._get_current_branch_context()


def test_an_empty_repository_is_unknown_as_locally(tmp_path: Path) -> None:
    world_local = tmp_path / "local" / "member"
    world_local.mkdir(parents=True)
    _git(world_local, "init", "-q", "-b", "main")
    shutil.copytree(world_local, tmp_path / "remote" / "member", symlinks=True)
    (tmp_path / "pod" / "member").mkdir(parents=True)
    backend = FakeGitMcp(tmp_path / "pod", tmp_path / "remote")
    local = GenericQueryService(world_local)._get_current_branch_context()
    delegated = GenericQueryService(
        tmp_path / "pod" / "member", git_backend=backend
    )._get_current_branch_context()
    assert (
        delegated == local == {"branch": "unknown", "commit": "unknown", "files": set()}
    )


# ── staleness: never guessed from an empty directory ────────────────────────


def test_staleness_reads_local_files_only_with_the_local_content_backend() -> None:
    """Delegated, the files are not here and the content backend has no
    per-file lookup: staleness is skipped (no indicator), never a stat of the
    pod's empty directory (every file "deleted") nor a whole-repository
    manifest per query."""
    assert cli_module._query_reads_local_files("local") is True
    assert cli_module._query_reads_local_files("delegated") is False


def test_the_query_git_backend_is_built_only_when_delegated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module, "_build_index_git_backend", lambda kind: f"git:{kind}"
    )
    assert cli_module._query_git_backend("local") is None
    assert cli_module._query_git_backend("delegated") == "git:delegated"


def test_cidx_query_accepts_the_hidden_backend_options() -> None:
    import click

    options = {
        p.name: p for p in cli_module.query.params if isinstance(p, click.Option)
    }
    for name in ("content_backend", "git_backend"):
        assert options[name].hidden is True
        assert options[name].default == "local"


# ── the server stamps the flags ─────────────────────────────────────────────


def _cli_args(monkeypatch: pytest.MonkeyPatch, content: str, git: str) -> List[str]:
    from code_indexer.server.query.semantic_query_manager import SemanticQueryManager
    from code_indexer.server.utils import index_command_layout as layout

    monkeypatch.setattr(layout, "configured_content_backend_name", lambda: content)
    monkeypatch.setattr(layout, "configured_git_backend_name", lambda: git)
    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    args: List[str] = manager._build_cli_args(query="auth", limit=5)
    return args


def test_the_server_stamps_delegated_backends_on_member_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _cli_args(monkeypatch, "delegated", "delegated")
    assert args[-2:] == ["--content-backend=delegated", "--git-backend=delegated"]


def test_the_local_member_query_command_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _cli_args(monkeypatch, "local", "local")
    assert args == ["query", "auth", "--quiet", "--limit", "5"]


def test_the_pod_path_is_not_resolved_on_this_pod(world: World, tmp_path: Path) -> None:
    """The member path is git-mcp's address: resolving it here (through a
    symlink on the pod) would ask git-mcp about a path it does not know."""
    alias_root = tmp_path / "pod-alias"
    alias_root.symlink_to(world.pod_root)
    service = GenericQueryService(alias_root / "member", git_backend=world.git_mcp)
    assert service.project_dir == alias_root / "member"


def _hits(paths: Sequence[str]) -> List[Dict[str, Any]]:
    return [
        {
            "score": 0.9,
            "payload": {
                "path": p,
                "line_start": 1,
                "content": "x",
                "indexed_at": "2020-01-01T00:00:00Z",
                "file_last_modified": 1.0,
            },
        }
        for p in paths
    ]


def test_delegated_query_staleness_stats_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.remote import staleness_detector

    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("staleness checked against a directory without files")

    monkeypatch.setattr(staleness_detector, "StalenessDetector", refuse)
    with pytest.MonkeyPatch.context() as patcher:
        world.forbid(patcher)
        assert (
            cli_module._query_staleness(_hits(["a.py"]), world.pod, "delegated") is None
        )


def test_local_query_staleness_is_unchanged(world: World) -> None:
    enhanced = cli_module._query_staleness(
        _hits(["a.py", "gone.py"]), world.local, "local"
    )
    assert enhanced is not None
    by_path = {r.file_path: r for r in enhanced}
    assert set(by_path) == {"a.py", "gone.py"}
    assert by_path["a.py"].is_stale is True  # edited after 2020
