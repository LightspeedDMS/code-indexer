"""Story #1600 AC5: REST status-code translation for the query-path
memory-pressure admission gate.

POST /api/query is the single REST endpoint backing BOTH search_code's
(semantic mode) and regex_search's (fts/hybrid mode) REST-facing
equivalent -- confirmed via code inspection: server/mcp/handlers/search.py's
search_code/handle_regex_search are pure MCP-dispatch functions with no
HTTP status control of their own; the only real REST route reachable
through the front door for either capability is this one
(routers/inline_query.py's semantic_query handler), which calls
semantic_query_manager.query_user_repositories()/TantivyIndexManager.search()
directly rather than through the MCP handler stack (see Story #1586
Finding 2's remediation note in this same file).

Unlike the MCP handlers (which always return the {"content": [...]}
envelope at HTTP 200), this REST route must translate a denied
AdmissionDecision into a literal HTTP 503 with a Retry-After header
(AC3/AC5).

Follows the established real-FastAPI-TestClient pattern from
test_inline_query_otel_metrics_wiring_1586.py and the real-governor
_StubGovernor/_StubConfigService seam pattern from
test_query_admission_gate_wiring_1600.py -- check_query_admission() itself
is never mocked.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.inline_query import register_query_routes
from code_indexer.server.services import config_service as cfg_service_module
from code_indexer.server.services import memory_governor as mg
from code_indexer.server.services.config_service import ConfigService

_WATERMARK_PCT = 80.0
_RED_MIN_DWELL_SECONDS = 17.3
_EXPECTED_RETRY_AFTER_SECONDS = math.ceil(_RED_MIN_DWELL_SECONDS)  # 18


def _make_user(username: str = "alice") -> User:
    user = MagicMock(spec=User)
    user.username = username
    user.role = UserRole.NORMAL_USER
    return user


class _StubGovernor:
    """Test double you control -- mirrors the established _StubGovernor
    pattern (test_background_jobs_admission_gate.py,
    test_query_admission_gate_wiring_1600.py). check_query_admission()
    itself is never mocked; it runs for real against this stub."""

    def __init__(self, allowed: bool) -> None:
        self._allowed = allowed
        self.last_red_min_dwell_seconds = _RED_MIN_DWELL_SECONDS
        self.increment_calls = 0
        self.admission_calls: List[float] = []

    def admission_allowed(self, max_used_pct: float) -> bool:
        self.admission_calls.append(max_used_pct)
        return self._allowed

    def increment_query_admissions_denied(self) -> None:
        self.increment_calls += 1


class _StubBackgroundJobsConfig:
    job_admission_memory_max_used_pct = _WATERMARK_PCT
    # B1 fix (kill switch, code review remediation): check_query_admission()
    # now reads this flag before consulting the governor at all. Must be
    # True here so this stub keeps behaving like the gate enabled by
    # default in production -- omitting it would raise AttributeError on
    # the real check_query_admission() call, which its own outer
    # try/except turns into an unintended fail-open (allowed=True) on
    # every DENIED test in this file.
    job_admission_memory_gate_enabled = True


class _StubServerConfig:
    background_jobs_config = _StubBackgroundJobsConfig()


class _StubConfigService(ConfigService):
    """Subclasses the real ConfigService (rather than a bare object +
    `type: ignore`) so set_config_service()'s ConfigService-typed parameter
    is satisfied without suppressing type checking. __init__ is
    deliberately NOT called (no filesystem/DB bootstrap needed) -- only
    get_config() is overridden, which is all check_query_admission() reads.
    """

    def __init__(self) -> None:  # noqa: super-init-not-called -- see docstring
        pass

    def get_config(self) -> _StubServerConfig:  # type: ignore[override]
        return _StubServerConfig()


@pytest.fixture(autouse=True)
def _admission_gate_seams():
    cfg_service_module.set_config_service(_StubConfigService())
    yield
    mg.clear_memory_governor()
    cfg_service_module.reset_config_service()


def _new_app(tmp_path: Path):
    activated_repos_dir = tmp_path / "activated-repos"

    mock_activated_repo_manager = MagicMock()
    mock_activated_repo_manager.get_activated_repos.return_value = []
    mock_activated_repo_manager.activated_repos_dir = str(activated_repos_dir)

    mock_semantic_query_manager = MagicMock()
    mock_semantic_query_manager.query_user_repositories.return_value = {
        "results": [],
        "total_results": 0,
        "query_metadata": {},
        "warning": None,
    }

    fast_app = FastAPI()
    fast_app.state.payload_cache = None
    fast_app.state.access_filtering_service = None
    fast_app.state.search_event_log_writer = None
    fast_app.state.query_tracker = None

    register_query_routes(
        fast_app,
        semantic_query_manager=mock_semantic_query_manager,
        activated_repo_manager=mock_activated_repo_manager,
    )

    from code_indexer.server.auth import dependencies as auth_deps

    fast_app.dependency_overrides[auth_deps.get_current_user] = lambda: _make_user(
        "alice"
    )
    return fast_app, mock_semantic_query_manager


class TestRestQueryAdmissionGateDenied:
    def test_denied_returns_503_with_retry_after_header(self, tmp_path: Path):
        stub_gov = _StubGovernor(allowed=False)
        mg.set_memory_governor(stub_gov)

        fast_app, mock_sqm = _new_app(tmp_path)
        with TestClient(fast_app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/query",
                json={"query_text": "find auth", "repository_alias": "myrepo"},
            )

        assert response.status_code == 503, response.text
        assert response.headers["retry-after"] == str(_EXPECTED_RETRY_AFTER_SECONDS)
        body = response.json()
        # FastAPI wraps HTTPException.detail under "detail" in the JSON body.
        detail = body.get("detail", body)
        assert detail["error_code"] == "memory_pressure"
        assert detail["success"] is False
        # Real work must NOT have run.
        mock_sqm.query_user_repositories.assert_not_called()

    def test_denied_consults_the_real_governor_exactly_once(self, tmp_path: Path):
        stub_gov = _StubGovernor(allowed=False)
        mg.set_memory_governor(stub_gov)

        fast_app, _mock_sqm = _new_app(tmp_path)
        with TestClient(fast_app, raise_server_exceptions=False) as client:
            client.post(
                "/api/query",
                json={"query_text": "find auth", "repository_alias": "myrepo"},
            )

        assert stub_gov.increment_calls == 1
        assert stub_gov.admission_calls == [_WATERMARK_PCT]


class TestRestQueryAdmissionGateAllowed:
    def test_allowed_proceeds_to_real_semantic_query(self, tmp_path: Path):
        mg.clear_memory_governor()  # fail-open: no governor installed

        fast_app, mock_sqm = _new_app(tmp_path)
        with TestClient(fast_app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/query",
                json={"query_text": "find auth", "repository_alias": "myrepo"},
            )

        assert response.status_code == 200, response.text
        mock_sqm.query_user_repositories.assert_called_once()
        # H5 (Story #1600 review remediation, AC Scenario 1): a genuine
        # allowed/successful response must carry NEITHER admission-decision
        # field at all -- not merely error_code != "memory_pressure" (which
        # would still pass even if the gate leaked a stray
        # retry_after_seconds: null into every successful response).
        body = response.json()
        assert "retry_after_seconds" not in body
        assert "error_code" not in body
