"""
Integration tests for Story #1157: Discovery background job flow.

Uses real BackgroundJobManager (in-memory, no DB) with mocked provider.
Results stored in real PayloadCache (SQLite in-memory via tmp path).

1. Full flow: POST start -> poll until completed -> GET result returns full repos list
2. PayloadCache allows re-reads within TTL (not read-once; TTL handles cleanup)
3. Dedup: second POST while first PENDING returns same job_id
"""

import time
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client_and_bgm():
    """TestClient with admin session + real BackgroundJobManager + real PayloadCache."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from code_indexer.server.web.routes import web_router
    from code_indexer.server.repositories.background_jobs import BackgroundJobManager
    from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig

    app = FastAPI()
    app.include_router(web_router, prefix="/admin")

    bgm = BackgroundJobManager()

    # Wire a real PayloadCache backed by a temp SQLite file
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_path = Path(tmpdir) / "test_payload_cache.db"
        payload_cache = PayloadCache(cache_path, PayloadCacheConfig())
        payload_cache.initialize()
        app.state.payload_cache = payload_cache

        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.role = "admin"
        mock_session.username = "admin"
        mock_sm.get_session.return_value = mock_session

        # Patch the name as imported into routes module
        with patch(
            "code_indexer.server.web.routes.get_session_manager", return_value=mock_sm
        ):
            client = TestClient(app, raise_server_exceptions=False)
            yield client, bgm


def _make_mock_provider(repos=None, total_source=3):
    """Build a mock provider that returns canned data from discover_all_repositories."""
    if repos is None:
        repos = [
            {
                "platform": "gitlab",
                "name": "repo1",
                "description": "",
                "clone_url_https": "https://gitlab.com/group/repo1.git",
                "clone_url_ssh": "git@gitlab.com:group/repo1.git",
                "default_branch": "main",
                "is_hidden": False,
                "is_private": False,
            }
        ]
    provider = MagicMock()
    provider.is_configured.return_value = True
    provider._get_indexed_canonical_urls.return_value = set()
    provider.discover_all_repositories.return_value = {
        "repositories": repos,
        "total_source": total_source,
        "total_unregistered": len(repos),
    }
    return provider


# ---------------------------------------------------------------------------
# IT-01: Full flow: POST start -> poll jobs endpoint -> GET result
# ---------------------------------------------------------------------------


class TestFullDiscoveryFlow:
    """Complete job lifecycle: submit -> complete -> consume result."""

    def test_full_flow_result_contains_repos(self, admin_client_and_bgm):
        """POST start -> wait for completion -> GET result returns repositories list."""
        client, bgm = admin_client_and_bgm
        mock_provider = _make_mock_provider()

        with (
            patch(
                "code_indexer.server.web.routes._get_background_job_manager",
                return_value=bgm,
            ),
            patch(
                "code_indexer.server.web.routes._get_gitlab_provider",
                return_value=mock_provider,
            ),
            patch(
                "code_indexer.server.web.routes._load_hidden_ids",
                return_value=(set(), set()),
            ),
        ):
            # POST to start discovery
            resp = client.post(
                "/admin/api/discovery/gitlab/start",
                follow_redirects=False,
            )
            assert resp.status_code == 200, (
                f"POST start failed: {resp.status_code} {resp.text}"
            )
            body = resp.json()
            job_id = body["job_id"]
            assert job_id, "No job_id returned"

            # Poll until completed (real BGM executes in background thread)
            max_wait = 10  # seconds
            start = time.time()
            job_status = None
            while time.time() - start < max_wait:
                job = bgm.jobs.get(job_id)
                if job and job.status.value in (
                    "completed",
                    "failed",
                    "completed_partial",
                ):
                    job_status = job.status.value
                    break
                time.sleep(0.1)

            assert job_status in ("completed", "completed_partial"), (
                f"Job did not complete in time, status={job_status}"
            )

            # GET result
            result_resp = client.get(
                f"/admin/api/discovery/gitlab/result/{job_id}",
                follow_redirects=False,
            )
            assert result_resp.status_code == 200, (
                f"GET result failed: {result_resp.status_code} {result_resp.text}"
            )
            result_body = result_resp.json()
            assert "repositories" in result_body, "Missing 'repositories' key"
            assert isinstance(result_body["repositories"], list)


# ---------------------------------------------------------------------------
# IT-02: PayloadCache allows re-reads within TTL (not read-once)
# ---------------------------------------------------------------------------


class TestPayloadCacheReads:
    """Result stored in PayloadCache can be read multiple times within TTL."""

    def test_second_get_result_also_200(self, admin_client_and_bgm):
        """After retrieving result once, second GET within TTL also returns 200."""
        client, bgm = admin_client_and_bgm
        mock_provider = _make_mock_provider()

        with (
            patch(
                "code_indexer.server.web.routes._get_background_job_manager",
                return_value=bgm,
            ),
            patch(
                "code_indexer.server.web.routes._get_gitlab_provider",
                return_value=mock_provider,
            ),
            patch(
                "code_indexer.server.web.routes._load_hidden_ids",
                return_value=(set(), set()),
            ),
        ):
            # POST to start
            resp = client.post(
                "/admin/api/discovery/gitlab/start",
                follow_redirects=False,
            )
            assert resp.status_code == 200
            job_id = resp.json()["job_id"]

            # Wait for completion
            max_wait = 10
            start = time.time()
            while time.time() - start < max_wait:
                job = bgm.jobs.get(job_id)
                if job and job.status.value in (
                    "completed",
                    "failed",
                    "completed_partial",
                ):
                    break
                time.sleep(0.1)

            # First GET - should succeed
            r1 = client.get(
                f"/admin/api/discovery/gitlab/result/{job_id}", follow_redirects=False
            )
            assert r1.status_code == 200, (
                f"First GET should be 200, got {r1.status_code}"
            )

            # Second GET - also 200 (PayloadCache is TTL-based, not read-once)
            r2 = client.get(
                f"/admin/api/discovery/gitlab/result/{job_id}", follow_redirects=False
            )
            assert r2.status_code == 200, (
                f"Second GET should also be 200 (TTL-based, not read-once), got {r2.status_code}"
            )


# ---------------------------------------------------------------------------
# IT-03: Dedup: second POST while first is PENDING returns same job_id
# ---------------------------------------------------------------------------


class TestDedupDuringPendingJob:
    """When a job is in PENDING state, second POST returns same job_id."""

    def test_second_post_returns_same_job_id(self, admin_client_and_bgm):
        """Two POSTs for same platform return same job_id while first is pending."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
            JobStatus,
            BackgroundJob,
        )
        from datetime import datetime, timezone

        client, _ = admin_client_and_bgm

        # Use a fresh BGM with a pre-inserted PENDING job (no real execution)
        bgm_dedup = BackgroundJobManager()

        existing_job_id = "dedup-pending-job"
        job = BackgroundJob(
            job_id=existing_job_id,
            operation_type="gitlab_discovery",
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="admin",
            is_admin=True,
            repo_alias=None,
            actor_username="admin",
        )
        bgm_dedup.jobs[existing_job_id] = job

        mock_provider = _make_mock_provider()
        mock_provider.is_configured.return_value = True

        with (
            patch(
                "code_indexer.server.web.routes._get_background_job_manager",
                return_value=bgm_dedup,
            ),
            patch(
                "code_indexer.server.web.routes._get_gitlab_provider",
                return_value=mock_provider,
            ),
            patch(
                "code_indexer.server.web.routes._load_hidden_ids",
                return_value=(set(), set()),
            ),
        ):
            resp = client.post(
                "/admin/api/discovery/gitlab/start",
                follow_redirects=False,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == existing_job_id
        assert body["existing"] is True
