"""Epic #2103 item 15: forge API URLs are derived from a repository's clone
URL through the single git URL parser.

The pull-request clients run on real local git repositories. The forge API
itself is an external service: its request is captured at the HTTP client
and refused, so no request leaves the process.
"""

import subprocess
from pathlib import Path
from typing import Any, List

import httpx
import pytest

from code_indexer.server.mcp.handlers.cicd import _derive_base_url_from_repo_url
from code_indexer.server.services.git_state_manager import (
    GitHubPRClient,
    GitLabPRClient,
)

SECRET = "s3cr3t-value"


def _repo_with_origin(path: Path, origin_url: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", origin_url], cwd=path, check=True)
    return path


@pytest.fixture
def forge_requests(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """URLs the clients tried to reach; every request is refused."""
    urls: List[str] = []

    def refuse(self: httpx.Client, url: Any, **kwargs: Any) -> httpx.Response:
        urls.append(str(url))
        raise httpx.ConnectError("forge API is not reachable from tests")

    monkeypatch.setattr(httpx.Client, "post", refuse)
    return urls


@pytest.mark.parametrize(
    "origin,api_path",
    [
        ("git@github.com:acme/repo.git", "/repos/acme/repo/pulls"),
        # Only a ".git" suffix is removed (".github" stays in the name).
        (
            f"https://example-user:{SECRET}@github.com/acme/widget.github.io.git",
            "/repos/acme/widget.github.io/pulls",
        ),
    ],
)
def test_github_pull_request_targets_the_parsed_owner_and_repo(
    tmp_path: Path, forge_requests: List[str], origin: str, api_path: str
) -> None:
    client = GitHubPRClient(_repo_with_origin(tmp_path / "r", origin), "pat")

    with pytest.raises(Exception):
        client.create_pull_request("t", "b", "feature", "main")

    assert forge_requests == [f"https://api.github.com{api_path}"]


@pytest.mark.parametrize(
    "origin,project",
    [
        ("git@gitlab.com:group/project.git", "group%2Fproject"),
        # Subgroups are part of the project path.
        ("git@gitlab.com:group/sub/project.git", "group%2Fsub%2Fproject"),
        (
            f"https://example-user:{SECRET}@gitlab.com/group/sub/project.git",
            "group%2Fsub%2Fproject",
        ),
    ],
)
def test_gitlab_merge_request_targets_the_parsed_project_path(
    tmp_path: Path, forge_requests: List[str], origin: str, project: str
) -> None:
    client = GitLabPRClient(_repo_with_origin(tmp_path / "r", origin), "pat")

    with pytest.raises(Exception):
        client.create_merge_request("t", "b", "feature", "main")

    assert forge_requests == [
        f"https://gitlab.com/api/v4/projects/{project}/merge_requests"
    ]
    assert SECRET not in forge_requests[0]


@pytest.mark.parametrize(
    "repo_url,platform,base_url",
    [
        (
            f"https://example-user:{SECRET}@gitlab.example.com/g/p.git",
            "gitlab",
            "https://gitlab.example.com",
        ),
        (
            "http://gitlab.example.com:8080/g/p",
            "gitlab",
            "http://gitlab.example.com:8080",
        ),
        ("https://github.example.com/o/r.git", "github", "https://github.example.com"),
        # Only http(s) clone URLs name the API host; others use the default.
        ("git@gitlab.example.com:g/p.git", "gitlab", "https://gitlab.com"),
        ("ssh://git@gitlab.example.com/g/p.git", "gitlab", "https://gitlab.com"),
        ("HTTPS://gitlab.example.com/g/p.git", "gitlab", "https://gitlab.com"),
        ("not-a-url", "github", "https://github.com"),
    ],
)
def test_forge_base_url_is_the_parsed_web_host(
    repo_url: str, platform: str, base_url: str
) -> None:
    assert _derive_base_url_from_repo_url(repo_url, platform) == base_url


@pytest.mark.parametrize(
    "repo_url",
    [
        f"https://example-user:{SECRET}@gitlab.example.com:99999/g/p.git",
        "https://gitlab.example.com:08443/g/p.git",
        "https://gitläb.example.com/g/p.git",
        "http://gitlab.example.com:bad/g/p.git",
    ],
)
def test_rejected_http_url_raises_instead_of_defaulting(repo_url: str) -> None:
    with pytest.raises(ValueError) as raised:
        _derive_base_url_from_repo_url(repo_url, "gitlab")

    assert str(raised.value).startswith("Cannot parse repo URL")
    assert SECRET not in str(raised.value)
