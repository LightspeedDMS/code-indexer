"""Story #1457 HIGH #10 (2026-07-23 code review): the activated-repos
router still defaults/validates "temporal" as a supported index type,
contradicting AC12's absolute rule (activated_repo_index_manager.py:207)
that temporal indexing is NEVER supported for activated repositories --
temporal data is owned exclusively by the golden repo's shared sister
location.

Bare FastAPI app exposing only the router under test, auth dependency
overridden. `_get_activated_repo_manager()`/`_get_background_job_manager()`
read from the GLOBAL `code_indexer.server.app.app.state` (not the local
test app's state), so they are patched directly at the module level
rather than via app.state assignment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.activated_repos import router


@pytest.fixture
def app_with_router():
    app = FastAPI()
    app.include_router(router)

    test_user = User(
        username="alice",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    app.dependency_overrides[get_current_user_hybrid] = lambda: test_user
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(app_with_router):
    return TestClient(app_with_router, raise_server_exceptions=False)


def test_reindex_default_index_types_excludes_temporal(client, tmp_path):
    """POST .../reindex with NO index_types must default to only the
    types AC12 actually supports for activated repos -- never 'temporal'."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()

    activated_manager = MagicMock()
    activated_manager.get_activated_repo_path.return_value = str(repo_dir)
    job_manager = MagicMock()
    job_manager.submit_job.return_value = "job-123"

    with (
        patch(
            "code_indexer.server.routers.activated_repos._get_activated_repo_manager",
            return_value=activated_manager,
        ),
        patch(
            "code_indexer.server.routers.activated_repos._get_background_job_manager",
            return_value=job_manager,
        ),
    ):
        response = client.post("/api/activated-repos/my-repo/reindex", json={})

    assert response.status_code == 202, response.text
    body = response.json()
    assert "temporal" not in body["index_types"], (
        "the default reindex index_types must NEVER include 'temporal' "
        "for an activated repository (AC12) -- got "
        f"{body['index_types']}"
    )


def test_add_index_type_rejects_temporal(client):
    """POST .../indexes/temporal must be rejected -- temporal is never a
    valid index type to add to an activated repository."""
    response = client.post("/api/activated-repos/my-repo/indexes/temporal")

    assert response.status_code == 400, response.text
    assert "temporal" in response.json()["detail"].lower()


def test_reindex_explicit_temporal_in_index_types_is_rejected(client, tmp_path):
    """Story #1457 HIGH #10 remaining gap (2026-07-24 re-review, Codex):
    the route no longer *defaults* to temporal, but an EXPLICIT
    {"index_types": ["temporal"]} body must ALSO be rejected loudly (AC12)
    -- not silently scheduled as a no-op placeholder job. This test
    asserts the TARGET (post-fix) behavior -- HTTP 400, no job submitted
    -- and is expected to currently FAIL (RED) since the route today
    accepts this body and returns 202 with a no-op job."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()

    activated_manager = MagicMock()
    activated_manager.get_activated_repo_path.return_value = str(repo_dir)
    job_manager = MagicMock()
    job_manager.submit_job.return_value = "job-123"

    with (
        patch(
            "code_indexer.server.routers.activated_repos._get_activated_repo_manager",
            return_value=activated_manager,
        ),
        patch(
            "code_indexer.server.routers.activated_repos._get_background_job_manager",
            return_value=job_manager,
        ),
    ):
        response = client.post(
            "/api/activated-repos/my-repo/reindex",
            json={"index_types": ["temporal"]},
        )

    assert response.status_code == 400, response.text
    assert "temporal" in response.json()["detail"].lower()
    # no reindex job must ever be submitted when temporal is explicitly
    # requested for an activated repository
    job_manager.submit_job.assert_not_called()


def test_reindex_case_variant_temporal_is_rejected(client, tmp_path):
    """2026-07-24 round-4 re-review (Codex): a bare lowercase-string check
    (`"temporal" in request.index_types`) is trivially bypassed by a case
    variant like "Temporal". This test asserts the TARGET (post-fix)
    behavior -- HTTP 400, no job submitted -- and is expected to
    currently FAIL (RED) since the route today only rejects the exact
    lowercase string."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()

    activated_manager = MagicMock()
    activated_manager.get_activated_repo_path.return_value = str(repo_dir)
    job_manager = MagicMock()
    job_manager.submit_job.return_value = "job-123"

    with (
        patch(
            "code_indexer.server.routers.activated_repos._get_activated_repo_manager",
            return_value=activated_manager,
        ),
        patch(
            "code_indexer.server.routers.activated_repos._get_background_job_manager",
            return_value=job_manager,
        ),
    ):
        response = client.post(
            "/api/activated-repos/my-repo/reindex",
            json={"index_types": ["Temporal"]},
        )

    assert response.status_code == 400, response.text
    assert "temporal" in response.json()["detail"].lower()
    job_manager.submit_job.assert_not_called()


def test_reindex_unsupported_index_type_is_rejected(client, tmp_path):
    """2026-07-24 round-4 re-review (Codex): validation must be an
    explicit allowlist check (semantic, fts, scip), not a hardcoded
    single-value "temporal" check -- any other typo'd/garbage value must
    also be rejected with HTTP 400, not silently scheduled."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()

    activated_manager = MagicMock()
    activated_manager.get_activated_repo_path.return_value = str(repo_dir)
    job_manager = MagicMock()
    job_manager.submit_job.return_value = "job-123"

    with (
        patch(
            "code_indexer.server.routers.activated_repos._get_activated_repo_manager",
            return_value=activated_manager,
        ),
        patch(
            "code_indexer.server.routers.activated_repos._get_background_job_manager",
            return_value=job_manager,
        ),
    ):
        response = client.post(
            "/api/activated-repos/my-repo/reindex",
            json={"index_types": ["bogus"]},
        )

    assert response.status_code == 400, response.text
    job_manager.submit_job.assert_not_called()


def test_reindex_mixed_case_valid_types_are_normalized_for_job_and_response(
    client, tmp_path
):
    """2026-07-24 round-5 re-review (Codex): validation lowercases entries
    ONLY for the allowlist check, but the job submission and response body
    previously used the RAW unnormalized request.index_types. This test
    asserts the TARGET (post-fix) behavior -- the SAME normalized
    (lowercased) list must be used for both -- and is expected to
    currently FAIL (RED) since the response today echoes the raw mixed
    casing ["Semantic", "FTS", "ScIp"] instead of
    ["semantic", "fts", "scip"]."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()

    activated_manager = MagicMock()
    activated_manager.get_activated_repo_path.return_value = str(repo_dir)
    job_manager = MagicMock()
    job_manager.submit_job.return_value = "job-123"

    with (
        patch(
            "code_indexer.server.routers.activated_repos._get_activated_repo_manager",
            return_value=activated_manager,
        ),
        patch(
            "code_indexer.server.routers.activated_repos._get_background_job_manager",
            return_value=job_manager,
        ),
    ):
        response = client.post(
            "/api/activated-repos/my-repo/reindex",
            json={"index_types": ["Semantic", "FTS", "ScIp"]},
        )

    assert response.status_code == 202, response.text
    assert response.json()["index_types"] == ["semantic", "fts", "scip"], (
        "the response must echo the NORMALIZED (lowercased) index types, "
        f"not the raw request casing -- got {response.json()['index_types']}"
    )
    job_manager.submit_job.assert_called_once()


def test_reindex_empty_index_types_list_is_rejected(client, tmp_path):
    """2026-07-24 round-5 re-review (Codex): an empty index_types list
    ([]) previously submitted a meaningless job with nothing to index.
    This test asserts the TARGET (post-fix) behavior -- HTTP 400, no job
    submitted -- and is expected to currently FAIL (RED) since the route
    today accepts an empty list and returns 202."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()

    activated_manager = MagicMock()
    activated_manager.get_activated_repo_path.return_value = str(repo_dir)
    job_manager = MagicMock()
    job_manager.submit_job.return_value = "job-123"

    with (
        patch(
            "code_indexer.server.routers.activated_repos._get_activated_repo_manager",
            return_value=activated_manager,
        ),
        patch(
            "code_indexer.server.routers.activated_repos._get_background_job_manager",
            return_value=job_manager,
        ),
    ):
        response = client.post(
            "/api/activated-repos/my-repo/reindex",
            json={"index_types": []},
        )

    assert response.status_code == 400, response.text
    job_manager.submit_job.assert_not_called()
