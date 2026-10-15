"""History tools on an ACTIVATED copy on a pod without it (content seam).

The history handlers (blame, file history, commit/diff search, show_commit,
the history git_log, file at revision) build the global_repos
``GitOperationsService``. Under ``content_backend: delegated`` they were handed
the git seam only for golden (``-global``) aliases: for a user's activated copy
the service probed a ``.git`` this pod does not have and answered "not a git
repository". The copy is on the shared volume, where git-mcp sees it, so these
tools now run through the same read-only funnel as golden repos (typed git-mcp
reads, the rest via ``git_exec`` with the read-only allowlist).

Pinned: parity with a local run, no local git, fail-loud, and the registry
(this user's activation) checked before any backend call.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from code_indexer.server.mcp.handlers import _legacy, _utils, git_read
from code_indexer.server.services import git_tools_seam
from code_indexer.server.startup import content_backend_wiring, git_backend_wiring
from tests.unit.server.services.test_golden_git_history_git_exec import (
    GitMcp,
    _forbid_local_git,
    build_history_repo,
    plugin,
)

ALIAS = "my-copy"

CALLS = [
    ("handle_git_blame", {"path": "renamed.txt"}),
    ("handle_git_blame", {"path": "renamed.txt", "start_line": 2, "end_line": 3}),
    ("handle_git_file_history", {"path": "renamed.txt"}),
    ("handle_git_search_commits", {"query": "needle"}),
    ("handle_git_search_commits", {"query": "e", "author": "Bob"}),
    ("handle_git_search_diffs", {"search_string": "needle"}),
    ("handle_git_search_diffs", {"search_pattern": "ne+dle", "is_regex": True}),
    ("handle_git_show_commit", {"commit_hash": "HEAD", "include_diff": True}),
    ("handle_git_show_commit", {"commit_hash": "HEAD~1"}),
    ("handle_git_log", {"path": "renamed.txt"}),
    ("handle_git_log", {"author": "Bob"}),
    ("handle_git_log", {"limit": 3}),
    ("handle_git_file_at_revision", {"path": "renamed.txt", "revision": "HEAD~1"}),
]


def _payload(response: Any) -> Any:
    data = json.loads(response["content"][0]["text"])
    return _timeless(data)


def _timeless(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _timeless(v) for k, v in value.items() if k != "search_time_ms"}
    if isinstance(value, list):
        return [_timeless(v) for v in value]
    return value


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The user's activated copy, as git-mcp sees it."""
    return build_history_repo(tmp_path_factory.mktemp("activated") / "copy")


class Activations:
    """The activation registry: which user has which copy, and where."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.asked: List[Any] = []

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        self.asked.append((username, alias))
        return (username, alias) == ("alice", ALIAS)

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        return str(self.path)


@pytest.fixture
def pod(tmp_path: Path) -> Path:
    path = tmp_path / "pod" / "activated-repos" / "alice" / ALIAS
    assert not path.exists()
    return path


@pytest.fixture
def git_mcp(repo: Path, pod: Path, monkeypatch: pytest.MonkeyPatch) -> GitMcp:
    server = GitMcp(pod, repo)
    monkeypatch.setattr(plugin.urllib.request, "urlopen", server.urlopen)
    return server


@pytest.fixture
def delegated(
    git_mcp: GitMcp, pod: Path, monkeypatch: pytest.MonkeyPatch
) -> Activations:
    backend = plugin.GitMcpBackend(base_url="http://git-mcp.test")
    monkeypatch.setattr(
        content_backend_wiring,
        "server_content_backend",
        lambda: SimpleNamespace(is_local=False),
    )
    monkeypatch.setattr(
        git_backend_wiring, "server_delegated_git_backend", lambda: backend
    )
    activations = Activations(pod)
    monkeypatch.setattr(_utils, "_get_activated_repo_manager", lambda: activations)
    return activations


def _local_payload(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tool: str, args: Dict[str, Any]
) -> Any:
    with monkeypatch.context() as m:
        m.setattr(content_backend_wiring, "server_content_backend", lambda: None)
        m.setattr(
            _legacy,
            "_resolve_git_repo_path",
            lambda alias, user, **kw: (str(repo), None),
        )
        return _payload(
            getattr(git_read, tool)(
                {"repository_alias": ALIAS, **args}, SimpleNamespace(username="alice")
            )
        )


@pytest.mark.parametrize("tool, args", CALLS)
def test_activated_history_matches_local_through_the_seam(
    repo: Path,
    delegated: Activations,
    git_mcp: GitMcp,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    args: Dict[str, Any],
) -> None:
    expected = _local_payload(repo, monkeypatch, tool, args)
    assert expected.get("success", True) is not False, expected
    _forbid_local_git(monkeypatch)
    actual = _payload(
        getattr(git_read, tool)(
            {"repository_alias": ALIAS, **args}, SimpleNamespace(username="alice")
        )
    )
    assert actual == expected
    assert git_mcp.calls, "git-mcp answered"


def test_activated_history_is_read_only(
    delegated: Activations, git_mcp: GitMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked: List[List[str]] = []
    real_check = git_tools_seam.require_read_only_git

    def check(cmd: Any) -> None:
        checked.append(list(cmd))
        real_check(cmd)

    monkeypatch.setattr(git_tools_seam, "require_read_only_git", check)
    data = _payload(
        git_read.handle_git_blame(
            {"repository_alias": ALIAS, "path": "renamed.txt"},
            SimpleNamespace(username="alice"),
        )
    )
    assert data.get("success", True) is not False, data
    assert checked and checked[-1][:2] == ["git", "blame"]


@pytest.mark.parametrize("tool, args", CALLS[:1] + CALLS[2:4] + CALLS[5:6])
def test_another_users_copy_is_not_found_before_any_backend_call(
    delegated: Activations, git_mcp: GitMcp, tool: str, args: Dict[str, Any]
) -> None:
    data = _payload(
        getattr(git_read, tool)(
            {"repository_alias": ALIAS, **args}, SimpleNamespace(username="mallory")
        )
    )
    assert data["success"] is False
    assert "not found" in data["error"]
    assert git_mcp.calls == []
    assert delegated.asked == [("mallory", ALIAS)]


@pytest.mark.parametrize("tool, args", CALLS[:1] + CALLS[2:4] + CALLS[9:10])
def test_a_backend_failure_is_an_error_not_an_empty_answer(
    delegated: Activations, git_mcp: GitMcp, tool: str, args: Dict[str, Any]
) -> None:
    git_mcp.unreachable = True
    data = _payload(
        getattr(git_read, tool)(
            {"repository_alias": ALIAS, **args}, SimpleNamespace(username="alice")
        )
    )
    assert data["success"] is False
    assert "git-mcp" in data["error"]
