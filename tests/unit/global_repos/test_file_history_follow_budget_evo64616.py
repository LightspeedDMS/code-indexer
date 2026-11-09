"""git_file_history reads without ``--follow`` first (issue-64616).

``--follow`` diffs every commit for rename detection; on a repository with
~10^5 commits that alone took tens of seconds per call. So the plain
``git log -- path`` runs first, and ``--follow`` runs only when the plain
history's OLDEST commit renamed the file into its path -- and then within
``FOLLOW_BUDGET_SECONDS``, falling back to the plain history. The result's
``follow_mode`` says which one answered.

Real git on a real repository; ``_run_git`` is wrapped (never replaced) only
to record the argv each case sends, or to stand in for a slow ``--follow``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.global_repos import git_operations
from code_indexer.global_repos.git_operations import (
    FOLLOW_BUDGET_SECONDS,
    FOLLOW_MODE_FOLLOW,
    FOLLOW_MODE_PLAIN,
    FOLLOW_MODE_PLAIN_FOLLOW_TIMED_OUT,
    GitOperationsService,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """file.py: created and modified twice, then renamed to renamed.py and
    modified once more; other.py: created and modified, never renamed."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "file.py").write_text("line one\nline two\nline three\n")
    _commit(path, "create file")
    (path / "file.py").write_text("line one\nline two\nline three\nfour\n")
    _commit(path, "modify file")
    (path / "other.py").write_text("other\n")
    _commit(path, "create other")
    (path / "other.py").write_text("other changed\n")
    _commit(path, "modify other")
    _git(path, "mv", "file.py", "renamed.py")
    _commit(path, "rename file")
    (path / "renamed.py").write_text("line one\nline two\nline three\nfour\nfive\n")
    _commit(path, "modify renamed")
    return path


class _Recorder:
    """Stands in for ``_run_git``: records each argv, then runs the real one
    unless a case asks the ``--follow`` run or the rename probe to fail."""

    def __init__(
        self,
        service: GitOperationsService,
        monkeypatch: pytest.MonkeyPatch,
        *,
        follow_raises: Optional[BaseException] = None,
        show_raises: Optional[BaseException] = None,
    ) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._real = service._run_git
        self._follow_raises = follow_raises
        self._show_raises = show_raises
        monkeypatch.setattr(service, "_run_git", self)

    def __call__(
        self, cmd: List[str], *, check: bool, timeout: Optional[float] = None
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append({"cmd": list(cmd), "timeout": timeout})
        if self._follow_raises is not None and "--follow" in cmd:
            raise self._follow_raises
        if self._show_raises is not None and cmd[1] == "show":
            raise self._show_raises
        return self._real(cmd, check=check, timeout=timeout)

    def ran(self, flag: str) -> bool:
        return any(flag in call["cmd"] for call in self.calls)

    def subcommands(self) -> List[str]:
        return [call["cmd"][1] for call in self.calls]


def _subjects(result: Any) -> List[str]:
    return [c.subject for c in result.commits]


def test_unrenamed_file_never_runs_follow(repo: Path, monkeypatch) -> None:
    service = GitOperationsService(repo)
    calls = _Recorder(service, monkeypatch)

    result = service.get_file_history("other.py")

    assert _subjects(result) == ["modify other", "create other"]
    assert result.follow_mode == FOLLOW_MODE_PLAIN
    assert result.renamed_from is None
    assert not calls.ran("--follow")
    # one plain log plus one single-commit rename probe, nothing else
    assert calls.subcommands() == ["log", "show"]


def test_renamed_file_follows_and_reports_it(repo: Path, monkeypatch) -> None:
    service = GitOperationsService(repo)
    calls = _Recorder(service, monkeypatch)

    result = service.get_file_history("renamed.py")

    assert _subjects(result) == [
        "modify renamed",
        "rename file",
        "modify file",
        "create file",
    ]
    assert result.follow_mode == FOLLOW_MODE_FOLLOW
    assert result.renamed_from == "file.py"
    follow = [c for c in calls.calls if "--follow" in c["cmd"]]
    assert len(follow) == 1
    assert 0 < follow[0]["timeout"] <= FOLLOW_BUDGET_SECONDS
    # the plain run went first, without --follow
    assert calls.calls[0]["cmd"][1] == "log"
    assert "--follow" not in calls.calls[0]["cmd"]


def test_follow_over_budget_returns_plain_history(repo: Path, monkeypatch) -> None:
    service = GitOperationsService(repo)
    _Recorder(
        service,
        monkeypatch,
        follow_raises=subprocess.TimeoutExpired(["git"], FOLLOW_BUDGET_SECONDS),
    )

    result = service.get_file_history("renamed.py")

    assert _subjects(result) == ["modify renamed", "rename file"]
    assert result.follow_mode == FOLLOW_MODE_PLAIN_FOLLOW_TIMED_OUT
    assert result.renamed_from == "file.py"
    assert result.truncated is False


def test_follow_failure_returns_plain_history(repo: Path, monkeypatch) -> None:
    service = GitOperationsService(repo)
    _Recorder(
        service,
        monkeypatch,
        follow_raises=subprocess.CalledProcessError(128, ["git"]),
    )

    result = service.get_file_history("renamed.py")

    assert _subjects(result) == ["modify renamed", "rename file"]
    assert result.follow_mode == FOLLOW_MODE_PLAIN


def test_follow_not_requested_runs_neither_probe_nor_follow(
    repo: Path, monkeypatch
) -> None:
    service = GitOperationsService(repo)
    calls = _Recorder(service, monkeypatch)

    result = service.get_file_history("renamed.py", follow_renames=False)

    assert _subjects(result) == ["modify renamed", "rename file"]
    assert result.follow_mode == FOLLOW_MODE_PLAIN
    assert calls.subcommands() == ["log"]


def test_truncated_plain_history_needs_no_follow(repo: Path, monkeypatch) -> None:
    """Over ``limit`` commits under the current name: those ARE the newest
    ``limit`` commits --follow would return, so it is never run."""
    service = GitOperationsService(repo)
    calls = _Recorder(service, monkeypatch)

    result = service.get_file_history("renamed.py", limit=1)

    assert _subjects(result) == ["modify renamed"]
    assert result.truncated is True
    assert result.follow_mode == FOLLOW_MODE_PLAIN
    assert calls.subcommands() == ["log"]


def test_rename_probe_failure_keeps_plain_history(repo: Path, monkeypatch) -> None:
    service = GitOperationsService(repo)
    calls = _Recorder(
        service,
        monkeypatch,
        show_raises=subprocess.TimeoutExpired(["git"], FOLLOW_BUDGET_SECONDS),
    )

    result = service.get_file_history("renamed.py")

    assert _subjects(result) == ["modify renamed", "rename file"]
    assert result.follow_mode == FOLLOW_MODE_PLAIN
    assert result.renamed_from is None
    assert not calls.ran("--follow")


def test_rename_probe_argv_is_golden_read_only(repo: Path, monkeypatch) -> None:
    """The probe's argv passes the read-only allowlist, so it runs through
    git-mcp git_exec on a pod without the repository."""
    from code_indexer.server.services.git_tools_seam import require_read_only_git

    service = GitOperationsService(repo)
    calls = _Recorder(service, monkeypatch)
    service.get_file_history("renamed.py")

    for call in calls.calls:
        require_read_only_git(call["cmd"])


def test_budget_is_bounded() -> None:
    assert 0 < git_operations.FOLLOW_BUDGET_SECONDS <= 30


def test_mcp_handler_reports_follow_mode(repo: Path) -> None:
    """The git_file_history tool answers with the mode the service used."""
    import json
    from datetime import datetime, timezone
    from unittest.mock import patch

    from code_indexer.server.auth.user_manager import User, UserRole
    from code_indexer.server.mcp.handlers.git_read import handle_git_file_history

    user = User(
        username="tester",
        password_hash="unused",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )
    with patch(
        "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
        return_value=(str(repo), None),
    ):
        response = handle_git_file_history(
            {"repository_alias": "example-repo", "path": "renamed.py"}, user
        )

    payload = json.loads(response["content"][0]["text"])
    assert payload["success"] is True, payload
    assert payload["follow_mode"] == FOLLOW_MODE_FOLLOW
    assert payload["renamed_from"] == "file.py"
    assert payload["total_count"] == 4


class _Clock:
    """A fake ``time.monotonic`` for git_operations; each git call advances it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _clocked(
    service: GitOperationsService,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    costs: Dict[str, float],
) -> List[Dict[str, Any]]:
    """Run the real git, then advance *clock* by the cost of that subcommand
    (``follow`` for a ``--follow`` log)."""
    calls: List[Dict[str, Any]] = []
    real = service._run_git

    def run(
        cmd: List[str], *, check: bool, timeout: Optional[float] = None
    ) -> "subprocess.CompletedProcess[str]":
        calls.append({"cmd": list(cmd), "timeout": timeout})
        result = real(cmd, check=check, timeout=None)
        kind = "follow" if "--follow" in cmd else cmd[1]
        clock.now += costs.get(kind, 0.0)
        if timeout is not None and costs.get(kind, 0.0) > timeout:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return result

    monkeypatch.setattr(git_operations.time, "monotonic", clock)
    monkeypatch.setattr(service, "_run_git", run)
    return calls


def test_probe_and_follow_share_one_deadline(repo: Path, monkeypatch) -> None:
    """A probe that takes 10 s leaves --follow only the remaining 5 s."""
    clock = _Clock()
    service = GitOperationsService(repo)
    calls = _clocked(service, monkeypatch, clock, {"show": 10.0, "follow": 1.0})

    result = service.get_file_history("renamed.py")

    probe = next(c for c in calls if c["cmd"][1] == "show")
    follow = next(c for c in calls if "--follow" in c["cmd"])
    assert probe["timeout"] == FOLLOW_BUDGET_SECONDS
    assert follow["timeout"] == pytest.approx(FOLLOW_BUDGET_SECONDS - 10.0)
    assert result.follow_mode == FOLLOW_MODE_FOLLOW


def test_follow_past_the_shared_deadline_returns_plain(repo: Path, monkeypatch) -> None:
    """Probe 10 s + --follow 10 s exceeds 15 s: the plain history answers,
    and the whole rename path never ran past the one budget."""
    clock = _Clock()
    service = GitOperationsService(repo)
    start = clock.now
    _clocked(service, monkeypatch, clock, {"show": 10.0, "follow": 10.0})

    result = service.get_file_history("renamed.py")

    assert result.follow_mode == FOLLOW_MODE_PLAIN_FOLLOW_TIMED_OUT
    assert _subjects(result) == ["modify renamed", "rename file"]
    assert clock.now - start <= FOLLOW_BUDGET_SECONDS + 10.0  # never 2x budget


def test_a_probe_that_spends_the_budget_never_starts_follow(
    repo: Path, monkeypatch
) -> None:
    clock = _Clock()
    service = GitOperationsService(repo)

    calls = _clocked(service, monkeypatch, clock, {})
    real_run = service._run_git

    def slow_probe(
        cmd: List[str], *, check: bool, timeout: Optional[float] = None
    ) -> "subprocess.CompletedProcess[str]":
        result = real_run(cmd, check=check, timeout=timeout)
        if cmd[1] == "show":
            clock.now += FOLLOW_BUDGET_SECONDS  # finished, but at the deadline
        return result

    monkeypatch.setattr(service, "_run_git", slow_probe)

    result = service.get_file_history("renamed.py")

    assert not any("--follow" in c["cmd"] for c in calls)
    assert result.follow_mode == FOLLOW_MODE_PLAIN_FOLLOW_TIMED_OUT


def test_a_path_that_never_existed_is_an_empty_plain_history(
    repo: Path, monkeypatch
) -> None:
    service = GitOperationsService(repo)
    calls = _Recorder(service, monkeypatch)

    result = service.get_file_history("never/existed.py")

    assert result.commits == [] and result.total_count == 0
    assert result.follow_mode == FOLLOW_MODE_PLAIN
    assert result.renamed_from is None
    assert calls.subcommands() == ["log"]  # no probe, no --follow
