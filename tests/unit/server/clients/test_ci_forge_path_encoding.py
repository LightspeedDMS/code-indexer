"""Forge clients validate CI identifiers and encode every URL path segment.

Defence in depth behind the CI handlers: GitHubActionsClient and
GitLabCIClient accept only positive-integer run/job/pipeline identifiers,
encode each interpolated path segment, and send query filters as encoded
query parameters. Requests go to a recording httpx.MockTransport (real httpx
URL handling, no network).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

import httpx
import pytest

from code_indexer.server.clients.ci_identifiers import (
    InvalidCIIdentifierError,
    encode_gitlab_project_id,
    parse_ci_id,
)
from code_indexer.server.clients.github_actions_client import (
    GitHubActionsClient,
    GitHubRepositoryNotFoundError,
)
from code_indexer.server.clients.gitlab_ci_client import (
    GitLabCIClient,
    GitLabProjectNotFoundError,
)

GITLAB_BASE_URL = "https://gitlab.example.com"
INVALID_IDS: List[Any] = [
    "../../../other-org/other-repo/actions/jobs/123",
    "5/..",
    "..",
    "5%2F..",
    "%2E%2E",
    " 5",
    "+5",
    "-5",
    "0",
    "５",
    -1,
    0,
    True,
    5.0,
    None,
    "",
    "9" * 20,
]


class Forge:
    """Recording fake forge: JSON bodies by raw path, 404 otherwise."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.routes: Dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.routes.get(request.url.raw_path.decode().split("?")[0])
        if body is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch) -> Forge:
    fake = Forge()
    transport = httpx.MockTransport(fake)
    real_async_client = httpx.AsyncClient

    def recording_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", recording_async_client)
    return fake


class TestParseCiId:
    @pytest.mark.parametrize("value,expected", [(1, 1), ("1", 1), ("0042", 42), (2**63 - 1, 2**63 - 1), (str(2**63 - 1), 2**63 - 1)])  # fmt: skip
    def test_positive_integers_accepted(self, value: Any, expected: int) -> None:
        assert parse_ci_id(value, "run_id") == expected

    @pytest.mark.parametrize("value", INVALID_IDS + [2**63, "1\n"], ids=repr)
    def test_non_positive_integers_refused(self, value: Any) -> None:
        with pytest.raises(InvalidCIIdentifierError) as exc:
            parse_ci_id(value, "run_id")
        assert str(exc.value) == "Invalid parameter: run_id must be a positive integer"


class TestGitLabProjectEncoding:
    @pytest.mark.parametrize(
        "project_id,expected",
        [
            ("example-org/example-repo", "example-org%2Fexample-repo"),
            ("example-org%2Fexample-repo", "example-org%2Fexample-repo"),
            ("example-org/sub.group/example_repo-1", "example-org%2Fsub.group%2Fexample_repo-1"),
            ("4242", "4242"),
            ("example-org/repo?x=1#y", "example-org%2Frepo%3Fx%3D1%23y"),
            ("a/b/c", "a%2Fb%2Fc"),
            ("a%2Fb", "a%2Fb"),
            ("a%2Fb/c", "a%2Fb%2Fc"),
        ],
    )  # fmt: skip
    def test_project_id_is_one_segment_encoded_once(
        self, project_id: str, expected: str
    ) -> None:
        assert encode_gitlab_project_id(project_id) == expected

    @pytest.mark.parametrize(
        "project_id",
        [
            "", ".", "..", "%2E%2E", "%2e",
            "group/../repo", "group/./repo", "./repo", "../repo", "group/..",
            "group//repo", "/repo", "repo/", "/", "//",
            "group%2F..%2Frepo", "group%2F%2Frepo", "%2Frepo", "repo%2F",
            "group/%2E%2E/repo",
        ],
    )  # fmt: skip
    def test_dot_or_empty_project_id_refused(self, project_id: str) -> None:
        with pytest.raises(ValueError):
            encode_gitlab_project_id(project_id)


GH_CALLS: Dict[str, Callable[[GitHubActionsClient, Any], Any]] = {
    "get_run": lambda c, i: c.get_run(repository="example-org/example-repo", run_id=i),
    "search_logs": lambda c, i: c.search_logs(repository="example-org/example-repo", run_id=i, pattern="x"),
    "get_job_logs": lambda c, i: c.get_job_logs(repository="example-org/example-repo", job_id=i),
    "retry_run": lambda c, i: c.retry_run(repository="example-org/example-repo", run_id=i),
    "cancel_run": lambda c, i: c.cancel_run(repository="example-org/example-repo", run_id=i),
}  # fmt: skip

GL_CALLS: Dict[str, Callable[[GitLabCIClient, Any], Any]] = {
    "get_pipeline": lambda c, i: c.get_pipeline(project_id="example-org/example-repo", pipeline_id=i),
    "search_logs": lambda c, i: c.search_logs(project_id="example-org/example-repo", pipeline_id=i, pattern="x"),
    "get_job_logs": lambda c, i: c.get_job_logs(project_id="example-org/example-repo", job_id=i),
    "retry_pipeline": lambda c, i: c.retry_pipeline(project_id="example-org/example-repo", pipeline_id=i),
    "cancel_pipeline": lambda c, i: c.cancel_pipeline(project_id="example-org/example-repo", pipeline_id=i),
}  # fmt: skip


class TestClientsRefuseInvalidIdentifiers:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", sorted(GH_CALLS))
    @pytest.mark.parametrize("bad_id", INVALID_IDS, ids=repr)
    async def test_github_client_refuses_before_request(
        self, forge: Forge, method: str, bad_id: Any
    ) -> None:
        with pytest.raises(InvalidCIIdentifierError):
            await GH_CALLS[method](GitHubActionsClient("t"), bad_id)
        assert forge.requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", sorted(GL_CALLS))
    @pytest.mark.parametrize("bad_id", INVALID_IDS, ids=repr)
    async def test_gitlab_client_refuses_before_request(
        self, forge: Forge, method: str, bad_id: Any
    ) -> None:
        client = GitLabCIClient("t", base_url=GITLAB_BASE_URL)
        with pytest.raises(InvalidCIIdentifierError):
            await GL_CALLS[method](client, bad_id)
        assert forge.requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("repository", ["example-org/..", "../example-repo", "example-org/.", "example-org/"])  # fmt: skip
    async def test_github_client_refuses_dot_repository_segments(
        self, forge: Forge, repository: str
    ) -> None:
        with pytest.raises(ValueError):
            await GitHubActionsClient("t").get_run(repository=repository, run_id=7)
        assert forge.requests == []

    @pytest.mark.asyncio
    async def test_github_search_logs_refuses_non_integer_forge_job_id(
        self, forge: Forge
    ) -> None:
        jobs_path = "/repos/example-org/example-repo/actions/runs/7/jobs"
        forge.routes[jobs_path] = {"jobs": [{"id": "1/../../x", "name": "build"}]}
        with pytest.raises(InvalidCIIdentifierError):
            await GitHubActionsClient("t").search_logs(
                repository="example-org/example-repo", run_id=7, pattern="x"
            )
        assert [r.url.raw_path.decode() for r in forge.requests] == [jobs_path]

    @pytest.mark.asyncio
    async def test_gitlab_search_logs_refuses_non_integer_forge_job_id(
        self, forge: Forge
    ) -> None:
        jobs_path = "/api/v4/projects/example-org%2Fexample-repo/pipelines/7/jobs"
        forge.routes[jobs_path] = [
            {"id": "1/../../x", "name": "build", "stage": "test"}
        ]
        with pytest.raises(InvalidCIIdentifierError):
            await GitLabCIClient("t", base_url=GITLAB_BASE_URL).search_logs(
                project_id="example-org/example-repo", pipeline_id=7, pattern="x"
            )
        assert [r.url.raw_path.decode() for r in forge.requests] == [jobs_path]


class TestClientsIgnoreResponseSuppliedUrls:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "jobs_url",
        [
            "https://api.github.com/repos/other-org/other-repo/actions/runs/7/jobs",
            "https://other.example.net/repos/example-org/example-repo/actions/runs/7/jobs",
        ],
    )
    async def test_github_get_run_never_requests_response_supplied_jobs_url(
        self, forge: Forge, jobs_url: str
    ) -> None:
        base = "/repos/example-org/example-repo/actions/runs/7"
        forge.routes[base] = {
            "id": 7,
            "name": "CI",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_sha": "abc",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:01:00Z",
            "run_started_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/example-org/example-repo/actions/runs/7",
            "jobs_url": jobs_url,
        }
        forge.routes[f"{base}/jobs"] = {"jobs": []}
        forge.routes[f"{base}/artifacts"] = {"artifacts": []}

        await GitHubActionsClient("t").get_run(
            repository="example-org/example-repo", run_id=7
        )

        requested = [str(r.url) for r in forge.requests]
        assert jobs_url not in requested
        assert requested == [
            f"https://api.github.com{base}",
            f"https://api.github.com{base}/jobs",
            f"https://api.github.com{base}/artifacts",
        ]


class TestClientsBuildExpectedUrls:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("project_id", ["example-org/example-repo", "example-org%2Fexample-repo"])  # fmt: skip
    async def test_gitlab_project_path_encoded_once(
        self, forge: Forge, project_id: str
    ) -> None:
        # The fake forge answers 404 for unrouted paths; only the URL matters.
        with pytest.raises(Exception, match="GitLab API error: 404"):
            await GitLabCIClient("t", base_url=GITLAB_BASE_URL).get_pipeline(
                project_id=project_id, pipeline_id=7
            )
        assert forge.requests[0].url.raw_path.decode() == (
            "/api/v4/projects/example-org%2Fexample-repo/pipelines/7"
        )

    @pytest.mark.asyncio
    async def test_github_search_logs_fetches_integer_job_logs(
        self, forge: Forge
    ) -> None:
        base = "/repos/example-org/example-repo/actions"
        forge.routes[f"{base}/runs/7/jobs"] = {"jobs": [{"id": 99, "name": "b"}]}
        forge.routes[f"{base}/jobs/99/logs"] = "ok\nERROR here\n"
        matches = await GitHubActionsClient("t").search_logs(
            repository="example-org/example-repo", run_id=7, pattern="error"
        )
        assert [m["line"] for m in matches] == ["ERROR here"]

    @pytest.mark.asyncio
    async def test_github_list_runs_sends_filters_as_query_parameters(
        self, forge: Forge
    ) -> None:
        with pytest.raises(GitHubRepositoryNotFoundError):
            await GitHubActionsClient("t").list_runs(
                repository="example-org/example-repo",
                branch="feature/a&status=success",
                status="failure",
            )
        url = forge.requests[0].url
        assert url.raw_path.decode().split("?")[0] == (
            "/repos/example-org/example-repo/actions/runs"
        )
        assert url.params.get_list("branch") == ["feature/a&status=success"]
        assert url.params.get_list("status") == ["failure"]

    @pytest.mark.asyncio
    async def test_gitlab_list_pipelines_sends_filters_as_query_parameters(
        self, forge: Forge
    ) -> None:
        with pytest.raises(GitLabProjectNotFoundError):
            await GitLabCIClient("t", base_url=GITLAB_BASE_URL).list_pipelines(
                project_id="example-org/example-repo",
                ref="main&status=success",
                status="failed",
            )
        url = forge.requests[0].url
        assert url.raw_path.decode().split("?")[0] == (
            "/api/v4/projects/example-org%2Fexample-repo/pipelines"
        )
        assert url.params.get_list("ref") == ["main&status=success"]
        assert url.params.get_list("status") == ["failed"]
