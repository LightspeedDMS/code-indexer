"""The PAT push names its remote by the credential-free URL: a resolved
remote URL that still carries userinfo never reaches the push command line
(the PAT is supplied through GIT_ASKPASS).

The remote is external, so the ``git push`` is intercepted at the
subprocess boundary; every local git command runs for real. Hosts,
usernames and secrets are neutral placeholders.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.git_operations_service import GitOperationsService

SECRET = "example-token-123"
STORED_URL = "https://example-user:example-token-123@git.example.com/owner/repo.git"
CREDENTIAL = {"token": "example-pat-456", "git_user_name": "Example"}


@pytest.fixture
def push_argvs(monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    pushes: List[List[str]] = []
    real_run = subprocess.run

    def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        argv = [str(part) for part in cmd]
        if argv[:2] == ["git", "push"]:
            pushes.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return pushes


def test_pat_push_argv_never_carries_stored_url_credentials(
    tmp_path: Path, push_argvs: List[List[str]]
) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", STORED_URL], check=True
    )
    service = GitOperationsService.__new__(GitOperationsService)
    service._git_timeouts = MagicMock(git_remote_timeout=60)

    service.git_push_with_pat(
        tmp_path,
        "origin",
        "main",
        CREDENTIAL,
        remote_url=STORED_URL,
        set_upstream=False,
    )

    assert push_argvs, "expected the push"
    for argv in push_argvs:
        assert "https://git.example.com/owner/repo.git" in argv, argv
        assert not any(SECRET in part for part in argv), argv
