"""Remote branch listing supplies the access token to git at run time, never
on a command line.

The remote is external, so the ``git ls-remote`` calls are intercepted at
the subprocess boundary: their argv and environment are recorded and they
answer a canned ref listing. Hosts and tokens are neutral placeholders.
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Tuple

import pytest

from code_indexer.server.services.remote_branch_service import RemoteBranchService

TOKEN = "example-pat-456"
CLONE_URL = "https://gitlab.example.com/group/repo.git"
_HEADS = "0123456789abcdef0123456789abcdef01234567\trefs/heads/main\n"
_SYMREF = "ref: refs/heads/main\tHEAD\n" + _HEADS.replace("refs/heads/main", "HEAD")


@pytest.fixture
def ls_remote_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> List[Tuple[List[str], Dict[str, str]]]:
    calls: List[Tuple[List[str], Dict[str, str]]] = []

    def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        argv = [str(part) for part in cmd]
        assert argv[:2] == ["git", "ls-remote"], argv
        calls.append((argv, dict(kwargs.get("env") or {})))
        stdout = _SYMREF if "--symref" in argv else _HEADS
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_branch_listing_supplies_credentials_at_run_time(
    ls_remote_calls: List[Tuple[List[str], Dict[str, str]]],
) -> None:
    result = RemoteBranchService().fetch_remote_branches(CLONE_URL, "gitlab", TOKEN)

    assert result.success, result.error
    assert result.branches == ["main"]
    assert result.default_branch == "main"
    assert len(ls_remote_calls) == 2, ls_remote_calls
    for argv, env in ls_remote_calls:
        assert CLONE_URL in argv, argv
        assert not any(TOKEN in part for part in argv), argv
        assert env.get("CIDX_GIT_REMOTE_USERNAME") == "oauth2"
        assert env.get("CIDX_GIT_REMOTE_PASSWORD") == TOKEN
