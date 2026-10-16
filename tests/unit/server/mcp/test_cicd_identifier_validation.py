"""CI run, job and pipeline identifiers are validated as positive integers.

Every CI tool (unified ci_* MCP tools, the gh_actions_* / gitlab_ci_*
handlers and their REST twins under /api/cicd) must refuse an identifier that
is not a positive integer BEFORE any credential is resolved and before any
request leaves the server, so a CI call can only address the repository the
caller was authorised for.

The forge is replaced by a recording httpx.MockTransport (real httpx request
construction and URL normalisation, no network). The credential resolvers are
replaced by fakes that GRANT a token and record the call: if an identifier
were not validated first, the resolver would run and a request would be
recorded, failing the zero-call assertions.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Tuple

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.mcp.handlers import cicd
from code_indexer.server.mcp.handlers.cicd import (
    _CiTarget,
    _LegacyCiCredential,
    handle_ci_cancel_run,
    handle_ci_get_job_logs,
    handle_ci_get_run,
    handle_ci_retry_run,
    handle_ci_search_logs,
    handle_gh_actions_cancel_run,
    handle_gh_actions_get_job_logs,
    handle_gh_actions_get_run,
    handle_gh_actions_retry_run,
    handle_gh_actions_search_logs,
    handle_gitlab_ci_cancel_pipeline,
    handle_gitlab_ci_get_job_logs,
    handle_gitlab_ci_get_pipeline,
    handle_gitlab_ci_retry_pipeline,
    handle_gitlab_ci_search_logs,
)

GITLAB_BASE_URL = "https://gitlab.example.com"
PROJECT = "example-org/example-repo"

# Identifiers that are not positive integers. Each one must be refused.
INVALID_IDS: List[Any] = [
    "../../../other-org/other-repo/actions/jobs/123",
    "1/../../other-org/other-repo",
    "..",
    "5/..",
    "5%2F..",
    "%2E%2E",
    "5%2Fjobs",
    " 5",
    "5 ",
    "5\n",
    "+5",
    "-5",
    "0",
    "1e3",
    "0x10",
    "１２",  # full-width digits
    "١٢",  # Arabic-Indic digits
    "1" * 30,
    -1,
    0,
    5.0,
    True,
    [5],
]


class Recorder:
    """Records resolver calls and outgoing forge requests."""

    def __init__(self) -> None:
        self.resolver_calls: List[Tuple[Any, ...]] = []
        self.requests: List[httpx.Request] = []


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.requests.append(request)
        return httpx.Response(404, json={"message": "Not Found"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def recording_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", recording_async_client)

    def fake_target(
        repository_alias: str, forge: str, user: Any, write: bool
    ) -> Tuple[_CiTarget, None]:
        rec.resolver_calls.append(("target", repository_alias, forge, write))
        if forge == "gitlab":
            target = _CiTarget(
                "gitlab", PROJECT, GITLAB_BASE_URL, "gitlab.example.com", "t"
            )
        else:
            target = _CiTarget("github", PROJECT, None, "github.com", "t")
        return target, None

    def fake_legacy(
        identifier: str,
        platform: str,
        user: Any,
        caller_base_url: Any,
        write: bool,
    ) -> Tuple[_LegacyCiCredential, None]:
        rec.resolver_calls.append(("legacy", identifier, platform, write))
        base_url = GITLAB_BASE_URL if platform == "gitlab" else None
        return _LegacyCiCredential("t", base_url), None

    monkeypatch.setattr(cicd, "_resolve_ci_target", fake_target)
    monkeypatch.setattr(cicd, "_resolve_legacy_ci_request", fake_legacy)
    return rec


class _User:
    username = "example-user"


def _payload(response: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(response["content"][0]["text"])  # type: ignore[no-any-return]


Handler = Callable[[Dict[str, Any], Any], Any]

# (tool label, handler, base args, identifier name, expected raw path for id 12345)
GH = "/repos/example-org/example-repo/actions"
GL = "/api/v4/projects/example-org%2Fexample-repo"
HANDLER_CASES: List[Tuple[str, Handler, Dict[str, Any], str, str]] = [
    ("ci_get_run/github", handle_ci_get_run, {"repository_alias": "r", "forge": "github"}, "run_id", f"{GH}/runs/12345"),
    ("ci_get_run/gitlab", handle_ci_get_run, {"repository_alias": "r", "forge": "gitlab"}, "run_id", f"{GL}/pipelines/12345"),
    ("ci_get_job_logs/github", handle_ci_get_job_logs, {"repository_alias": "r", "forge": "github"}, "job_id", f"{GH}/jobs/12345/logs"),
    ("ci_get_job_logs/gitlab", handle_ci_get_job_logs, {"repository_alias": "r", "forge": "gitlab"}, "job_id", f"{GL}/jobs/12345/trace"),
    ("ci_search_logs/github", handle_ci_search_logs, {"repository_alias": "r", "forge": "github", "pattern": "x"}, "run_id", f"{GH}/runs/12345/jobs"),
    ("ci_search_logs/gitlab", handle_ci_search_logs, {"repository_alias": "r", "forge": "gitlab", "pattern": "x"}, "run_id", f"{GL}/pipelines/12345/jobs"),
    ("ci_cancel_run/github", handle_ci_cancel_run, {"repository_alias": "r", "forge": "github"}, "run_id", f"{GH}/runs/12345/cancel"),
    ("ci_cancel_run/gitlab", handle_ci_cancel_run, {"repository_alias": "r", "forge": "gitlab"}, "run_id", f"{GL}/pipelines/12345/cancel"),
    ("ci_retry_run/github", handle_ci_retry_run, {"repository_alias": "r", "forge": "github"}, "run_id", f"{GH}/runs/12345/rerun"),
    ("ci_retry_run/gitlab", handle_ci_retry_run, {"repository_alias": "r", "forge": "gitlab"}, "run_id", f"{GL}/pipelines/12345/retry"),
    ("gh_get_run", handle_gh_actions_get_run, {"repository": PROJECT}, "run_id", f"{GH}/runs/12345"),
    ("gh_search_logs", handle_gh_actions_search_logs, {"repository": PROJECT, "pattern": "x"}, "run_id", f"{GH}/runs/12345/jobs"),
    ("gh_get_job_logs", handle_gh_actions_get_job_logs, {"repository": PROJECT}, "job_id", f"{GH}/jobs/12345/logs"),
    ("gh_retry_run", handle_gh_actions_retry_run, {"repository": PROJECT}, "run_id", f"{GH}/runs/12345/rerun"),
    ("gh_cancel_run", handle_gh_actions_cancel_run, {"repository": PROJECT}, "run_id", f"{GH}/runs/12345/cancel"),
    ("gl_get_pipeline", handle_gitlab_ci_get_pipeline, {"project_id": PROJECT}, "pipeline_id", f"{GL}/pipelines/12345"),
    ("gl_search_logs", handle_gitlab_ci_search_logs, {"project_id": PROJECT, "pattern": "x"}, "pipeline_id", f"{GL}/pipelines/12345/jobs"),
    ("gl_get_job_logs", handle_gitlab_ci_get_job_logs, {"project_id": PROJECT}, "job_id", f"{GL}/jobs/12345/trace"),
    ("gl_retry_pipeline", handle_gitlab_ci_retry_pipeline, {"project_id": PROJECT}, "pipeline_id", f"{GL}/pipelines/12345/retry"),
    ("gl_cancel_pipeline", handle_gitlab_ci_cancel_pipeline, {"project_id": PROJECT}, "pipeline_id", f"{GL}/pipelines/12345/cancel"),
]  # fmt: skip

CASE_IDS = [case[0] for case in HANDLER_CASES]


class TestMcpHandlersRefuseInvalidIdentifiers:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", HANDLER_CASES, ids=CASE_IDS)
    @pytest.mark.parametrize("bad_id", INVALID_IDS, ids=repr)
    async def test_invalid_identifier_refused_before_credentials_and_requests(
        self, recorder: Recorder, case: Tuple[Any, ...], bad_id: Any
    ) -> None:
        _label, handler, base_args, id_name, _path = case
        args = dict(base_args, **{id_name: bad_id})

        payload = _payload(await handler(args, _User()))

        assert payload == {
            "success": False,
            "error": f"Invalid parameter: {id_name} must be a positive integer",
        }
        assert recorder.resolver_calls == []
        assert recorder.requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", HANDLER_CASES, ids=CASE_IDS)
    @pytest.mark.parametrize("missing", [None, ""], ids=["absent", "empty"])
    async def test_missing_identifier_keeps_missing_parameter_error(
        self, recorder: Recorder, case: Tuple[Any, ...], missing: Any
    ) -> None:
        _label, handler, base_args, id_name, _path = case
        args = dict(base_args)
        if missing is not None:
            args[id_name] = missing

        payload = _payload(await handler(args, _User()))

        assert payload == {
            "success": False,
            "error": f"Missing required parameter: {id_name}",
        }
        assert recorder.resolver_calls == []
        assert recorder.requests == []


class TestMcpHandlersAcceptIntegerIdentifiers:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", HANDLER_CASES, ids=CASE_IDS)
    @pytest.mark.parametrize("good_id", [12345, "12345"], ids=["int", "str"])
    async def test_integer_identifier_reaches_expected_forge_path(
        self, recorder: Recorder, case: Tuple[Any, ...], good_id: Any
    ) -> None:
        _label, handler, base_args, id_name, expected_path = case
        args = dict(base_args, **{id_name: good_id})

        await handler(args, _User())

        assert len(recorder.resolver_calls) == 1
        assert recorder.requests, "a valid identifier must reach the forge"
        assert recorder.requests[0].url.raw_path.decode() == expected_path


# ---------------------------------------------------------------------------
# REST twins (/api/cicd/...) delegate to the gh_actions_* / gitlab_ci_*
# handlers; ids arrive as path segments.
# ---------------------------------------------------------------------------

REST_CASES: List[Tuple[str, str]] = [
    ("GET", "/api/cicd/github/example-org/example-repo/runs/{id}"),
    ("GET", "/api/cicd/github/example-org/example-repo/runs/{id}/logs"),
    ("GET", "/api/cicd/github/example-org/example-repo/jobs/{id}/logs"),
    ("POST", "/api/cicd/github/example-org/example-repo/runs/{id}/retry"),
    ("POST", "/api/cicd/github/example-org/example-repo/runs/{id}/cancel"),
    # A {project_id} path parameter cannot hold a decoded "/", so the GitLab
    # twins are addressed by numeric project id.
    ("GET", "/api/cicd/gitlab/4242/pipelines/{id}"),
    ("GET", "/api/cicd/gitlab/4242/pipelines/{id}/logs"),
    ("GET", "/api/cicd/gitlab/4242/jobs/{id}/logs"),
    ("POST", "/api/cicd/gitlab/4242/pipelines/{id}/retry"),
    ("POST", "/api/cicd/gitlab/4242/pipelines/{id}/cancel"),
]

# Refused by the handler (0, -5), by FastAPI's int path parameter (422), or by
# routing, because a decoded "/" cannot match one path segment (404).
REST_INVALID_IDS = ["0", "-5", "%2E%2E", "5%2F..", "5.5", "1e3", "%EF%BC%91"]


@pytest.fixture
def rest_client() -> TestClient:
    from code_indexer.server.auth.dependencies import get_current_user
    from code_indexer.server.routes.cicd import router as cicd_router

    app = FastAPI()

    async def current_user() -> _User:
        return _User()

    app.dependency_overrides[get_current_user] = current_user
    app.include_router(cicd_router)
    return TestClient(app)


class TestRestTwinsRefuseInvalidIdentifiers:
    @pytest.mark.parametrize("method,template", REST_CASES)
    @pytest.mark.parametrize("bad_id", REST_INVALID_IDS)
    def test_invalid_identifier_refused_before_credentials_and_requests(
        self,
        recorder: Recorder,
        rest_client: TestClient,
        method: str,
        template: str,
        bad_id: str,
    ) -> None:
        response = rest_client.request(method, template.format(id=bad_id))

        if response.status_code == 200:
            body = _payload(response.json())
            assert body["success"] is False
            assert "must be a positive integer" in body["error"]
        else:
            assert response.status_code in (404, 422)
        assert recorder.resolver_calls == []
        assert recorder.requests == []

    @pytest.mark.parametrize("method,template", REST_CASES)
    def test_integer_identifier_reaches_forge(
        self,
        recorder: Recorder,
        rest_client: TestClient,
        method: str,
        template: str,
    ) -> None:
        rest_client.request(method, template.format(id="12345"))

        assert len(recorder.resolver_calls) == 1
        assert recorder.requests
        assert "/12345" in recorder.requests[0].url.raw_path.decode()
