"""
Router-level regression test for Bug #1248.

POST /api/v1/repos/{alias}/reindex returned HTTP 500 for every successful
reindex submission because the route did:

    result = service.trigger_reindex(...)   # returns a job_id STRING (-> str)
    return TriggerReindexResponse(**result)  # ** on a str -> TypeError

`ActivatedRepoIndexManager.trigger_reindex` has always returned a bare job_id
string (`-> str`, see activated_repo_index_manager.py), never a mapping, so
`TriggerReindexResponse(**result)` always crashed with:

    TypeError: TriggerReindexResponse() argument after ** must be a mapping, not str

This was hidden because the unit tests for `trigger_reindex` call the service
method directly (asserting the str return) and never exercised the router's
response construction. This module adds that missing router-level coverage.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.indexing import router

TRIGGER_REINDEX_TARGET = (
    "code_indexer.server.services.activated_repo_index_manager."
    "ActivatedRepoIndexManager.trigger_reindex"
)


@pytest.fixture
def app_with_router():
    """Minimal FastAPI app exposing only the indexing router.

    Mirrors the pattern used in test_provider_indexes_auth.py: a bare app
    with just the router under test, avoiding the cost/fragility of booting
    the full server app for a router-construction-only test.
    """
    app = FastAPI()
    app.include_router(router)

    # Non-None so the route's 503 "not fully initialized" guard does not fire.
    app.state.background_job_manager = MagicMock()
    app.state.activated_repo_manager = MagicMock()

    test_user = User(
        username="alice",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    app.dependency_overrides[get_current_user] = lambda: test_user
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(app_with_router):
    return TestClient(app_with_router, raise_server_exceptions=False)


class TestTriggerReindexResponseConstruction1248:
    """Bug #1248: router must not do TriggerReindexResponse(**job_id_str)."""

    def test_reindex_returns_valid_response_when_trigger_reindex_returns_job_id_str(
        self, client
    ):
        """POST /reindex must build a full response from the job_id string.

        ActivatedRepoIndexManager.trigger_reindex's real contract is `-> str`
        (just the job_id). This mocks that exact contract and asserts the
        router builds a complete, schema-valid TriggerReindexResponse instead
        of crashing with "argument after ** must be a mapping, not str".
        """
        with patch(TRIGGER_REINDEX_TARGET, return_value="job-1248-abc123"):
            response = client.post(
                "/api/v1/repos/my-repo/reindex",
                json={"index_types": ["semantic", "fts"], "clear": True},
            )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["success"] is True
        assert body["job_id"] == "job-1248-abc123"
        assert isinstance(body["status"], str) and body["status"] != ""
        assert body["index_types"] == ["semantic", "fts"]
        # started_at must be a parseable ISO-8601 timestamp.
        datetime.fromisoformat(body["started_at"])

    def test_reindex_echoes_job_id_from_a_different_mocked_value(self, client):
        """job_id in the response must be exactly what trigger_reindex returned.

        Guards against a fix that hardcodes/forges job_id instead of
        threading through the real value from the service layer.
        """
        with patch(TRIGGER_REINDEX_TARGET, return_value="another-job-id-999"):
            response = client.post(
                "/api/v1/repos/other-repo/reindex",
                json={"index_types": ["temporal"], "clear": False},
            )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["job_id"] == "another-job-id-999"
        assert body["index_types"] == ["temporal"]
