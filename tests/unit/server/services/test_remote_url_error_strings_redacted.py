"""Errors that name a repository URL carry it with its userinfo redacted.

Real URL parsing, real git repositories and a real ``git ls-remote``
against an unreachable example host; no mocks.

Hosts and secrets are neutral placeholders.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from code_indexer.server.clients.forge_client import extract_owner_repo
from code_indexer.server.repositories.golden_repo_manager import (
    GitOperationError,
    GoldenRepoManager,
)
from code_indexer.server.services.git_state_manager import (
    GitHubPRClient,
    GitLabPRClient,
)
from code_indexer.server.services.git_url_normalizer import (
    GitUrlNormalizationError,
    GitUrlNormalizer,
)
from code_indexer.server.services.remote_branch_service import RemoteBranchService

SECRET = "example-token-123"
PAT = "example-pat-456"
ORIGIN = f"https://example-user:{SECRET}@git.example.com/o/r.git"


def _assert_redacted(message: str) -> None:
    """The message names the URL with its userinfo replaced by ``***``."""
    assert SECRET not in message, message
    assert "***@git.example.com" in message, message


@pytest.mark.parametrize(
    "url",
    [
        f"https://example-user:{SECRET}@git.example.com",
        f"https://example-user:{SECRET}@git.example.com/only-owner",
    ],
)
def test_owner_repo_parse_error_redacts_userinfo(url: str) -> None:
    with pytest.raises(ValueError) as raised:
        extract_owner_repo(url)
    message = str(raised.value)
    _assert_redacted(message)


def test_url_normalization_error_redacts_userinfo() -> None:
    with pytest.raises(GitUrlNormalizationError) as raised:
        GitUrlNormalizer().normalize(f"ftp://example-user:{SECRET}@git.example.com/r")
    message = str(raised.value)
    _assert_redacted(message)


@pytest.mark.parametrize(
    "clone_url,platform,credentials",
    [
        (f"https://example-user:{SECRET}@git.example.com:8443/o/r.git", None, None),
        ("https://git.example.com:8443/o/r.git", "gitlab", PAT),
    ],
)
def test_branch_fetch_error_redacts_userinfo(
    clone_url: str, platform: str, credentials: str, caplog: pytest.LogCaptureFixture
) -> None:
    result = RemoteBranchService(timeout=20).fetch_remote_branches(
        clone_url, platform=platform, credentials=credentials
    )
    assert result.success is False
    for secret in (SECRET, PAT):
        assert secret not in (result.error or ""), result.error
        assert secret not in caplog.text, caplog.text


def _repo_with_origin(path: Path, origin_url: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", origin_url], cwd=path, check=True)
    return path


def test_github_pr_client_error_redacts_userinfo(tmp_path: Path) -> None:
    client = GitHubPRClient(_repo_with_origin(tmp_path / "r", ORIGIN), PAT)
    with pytest.raises(Exception) as raised:
        client.create_pull_request("t", "b", "h", "main")
    message = str(raised.value)
    _assert_redacted(message)


def test_gitlab_pr_client_error_redacts_userinfo(tmp_path: Path) -> None:
    client = GitLabPRClient(_repo_with_origin(tmp_path / "r", ORIGIN), PAT)
    with pytest.raises(Exception) as raised:
        client.create_merge_request("t", "b", "h", "main")
    message = str(raised.value)
    _assert_redacted(message)


def test_committer_resolution_logs_unparseable_url_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.server.services.committer_resolution_service import (
        CommitterResolutionService,
    )
    from code_indexer.server.services.ssh_key_manager import SSHKeyManager

    service = CommitterResolutionService(
        ssh_key_manager=SSHKeyManager(
            ssh_dir=tmp_path / "ssh",
            metadata_dir=tmp_path / "meta",
            config_path=tmp_path / "ssh" / "config",
        )
    )
    with caplog.at_level(logging.DEBUG):
        email, key = service.resolve_committer_email(
            f"ftp://example-user:{SECRET}@git.example.com/o/r.git",
            "default@example.com",
        )
    assert (email, key) == ("default@example.com", None)
    assert SECRET not in caplog.text, caplog.text


def test_branch_fetch_timeout_logs_no_userinfo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        result = RemoteBranchService(timeout=0).fetch_remote_branches(ORIGIN)
    assert result.success is False
    assert SECRET not in (result.error or "")
    assert SECRET not in caplog.text, caplog.text


def test_accessibility_check_timeout_logs_no_userinfo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    manager.resource_config.git_clone_timeout = 0
    with caplog.at_level(logging.DEBUG):
        assert manager._validate_git_repository(ORIGIN) is False
    assert SECRET not in caplog.text, caplog.text


def test_add_golden_repo_inaccessible_error_redacts_userinfo(tmp_path: Path) -> None:
    manager = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    unreachable = f"https://example-user:{SECRET}@git.example.com:8443/o/r.git"
    with pytest.raises(GitOperationError) as raised:
        manager.add_golden_repo(
            repo_url=unreachable,
            alias="unreachable-repo",
            submitter_username="example_admin",
        )
    message = str(raised.value)
    _assert_redacted(message)
