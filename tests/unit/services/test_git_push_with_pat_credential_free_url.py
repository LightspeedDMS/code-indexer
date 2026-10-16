"""The PAT push names its remote, never a URL: a resolved remote URL that
still carries userinfo never reaches the push command line. The push
travels over the credential-free URL (a run-time rewrite) and the PAT
reaches git only through the push environment.

The remote is external, so the ``git push`` is intercepted at the
subprocess boundary; every local git command runs for real. Hosts,
usernames and secrets are neutral placeholders.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from code_indexer.server.services.git_operations_service import (
    git_operations_service,
)

SECRET = "example-token-123"
STORED_URL = "https://example-user:example-token-123@git.example.com/owner/repo.git"
PAT = "example-pat-456"
CREDENTIAL = {"token": PAT, "git_user_name": "Example"}


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[List[str], Dict[str, str]]]:
    recorded: List[Tuple[List[str], Dict[str, str]]] = []
    real_run = subprocess.run

    def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        argv = [str(part) for part in cmd]
        if argv[:2] == ["git", "push"]:
            recorded.append((argv, dict(kwargs.get("env") or {})))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return recorded


def test_pat_push_argv_never_carries_stored_url_credentials(
    tmp_path: Path, pushes: List[Tuple[List[str], Dict[str, str]]]
) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", STORED_URL], check=True
    )

    git_operations_service.git_push_with_pat(
        tmp_path,
        "origin",
        "main",
        CREDENTIAL,
        remote_url=STORED_URL,
        set_upstream=False,
    )

    assert [argv for argv, _env in pushes] == [
        ["git", "push", "--end-of-options", "origin", "HEAD:refs/heads/main"]
    ]
    env = pushes[0][1]
    run_time_config = [
        (env[key], env[key.replace("KEY", "VALUE")])
        for key in env
        if key.startswith("GIT_CONFIG_KEY_")
    ]
    assert run_time_config
    for key, value in run_time_config:
        assert SECRET not in key and SECRET not in value, key
    assert env["CIDX_GIT_REMOTE_PASSWORD"] == PAT
