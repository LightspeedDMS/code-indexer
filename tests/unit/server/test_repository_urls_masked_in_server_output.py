"""Repository URLs are masked in server log output and error text.

Every site is driven with a URL whose userinfo carries a secret
placeholder; the message or log line is still produced, and never holds the
secret. Local git repositories are real; no path makes a network call.
"""

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

SECRET = "s3cr3t-value"
REMOTE = f"https://example-user:{SECRET}@git.example.com/example/repo.git"


def _messages(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [record.getMessage() for record in caplog.records]


def test_duplicate_sync_error_message_omits_userinfo() -> None:
    from code_indexer.server.jobs.exceptions import DuplicateRepositorySyncError

    error = DuplicateRepositorySyncError(REMOTE, "job-1")

    assert "git.example.com/example/repo.git" in str(error)
    assert SECRET not in str(error)


class _FailingGoldenRepoManager:
    """Golden-repo manager whose add always fails."""

    def list_golden_repos(self) -> List[Dict[str, Any]]:
        return []

    def add_golden_repo(self, **kwargs: Any) -> str:
        raise RuntimeError("add refused")


def test_batch_create_failure_log_omits_userinfo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from code_indexer.server.web.routes import _batch_create_repos

    caplog.set_level(logging.WARNING)

    result = _batch_create_repos(
        [{"alias": "example-repo", "clone_url": REMOTE, "branch": "main"}],
        "example-admin",
        _FailingGoldenRepoManager(),
    )

    assert result["results"][0]["status"] == "failed"
    messages = _messages(caplog)
    assert any("Batch golden repo create failed" in m for m in messages)
    assert all(SECRET not in m for m in messages)


def test_fetch_decision_for_unrecognised_origin_omits_userinfo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    # An upper-case scheme falls outside the remote prefixes.
    origin = f"HTTPS://example-user:{SECRET}@git.example.com/example/repo.git"
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=tmp_path, check=True)
    manager = ActivatedRepoManager.__new__(ActivatedRepoManager)
    manager.logger = logging.getLogger("test.activated_repo_manager")
    caplog.set_level(logging.DEBUG)

    should_fetch, info = manager._should_fetch_from_remote(str(tmp_path))

    assert should_fetch is False
    assert info.startswith("Local repository: ")
    assert SECRET not in info
    assert all(SECRET not in m for m in _messages(caplog))


def test_local_scheme_registration_log_omits_userinfo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
    )

    manager = GoldenRepoManager.__new__(GoldenRepoManager)
    caplog.set_level(logging.INFO)
    target = tmp_path / "target"

    result = manager._clone_local_repository_with_regular_copy(
        f"local://example-user:{SECRET}@example-repo", str(target)
    )

    assert result == str(target)
    messages = _messages(caplog)
    assert any("local repository directory" in m for m in messages)
    assert all(SECRET not in m for m in messages)
