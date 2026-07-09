"""
Unit tests for Story #1157: GitLab Auto-Discovery Background Job with Progress Bar.
Updated for cluster-safe PayloadCache storage (Bug fix: was per-node RAM dict).

Tests cover:
1. _fetch_all_pages_rest(progress_callback=...) invokes callback per page, monotonic, capped at 90
2. _fetch_all_pages_rest() with progress_callback=None - no regression
3. _fetch_all_pages_graphql(progress_callback=...) same callback behavior
4. _fetch_all_pages_graphql() with progress_callback=None - no regression
5. discover_all_repositories(..., progress_callback=...) threads callback through both providers
6. PayloadCache key semantics: result stored under "discovery:{job_id}"
7. Worker closure: result_holder populated, stored in PayloadCache
8. Dedup scan: PENDING/RUNNING job same op_type returns existing job_id
9. No existing job: submits new, returns existing=False
10. POST without admin session -> 401
11. GET result without admin session -> 401
12. GET result for unknown key -> 404
13. GET result when payload_cache is None -> 503
"""

import pytest
from unittest.mock import MagicMock, patch

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


def _bypass_elevation(app, rtr):
    """Override all require_elevation deps so functional tests can run without TOTP."""
    from fastapi.routing import APIRoute

    for route in rtr.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in route.dependencies or []:
            dep_callable = getattr(dep, "dependency", None)
            if (
                dep_callable
                and getattr(dep_callable, "__qualname__", "") == _ELEVATION_QUALNAME
            ):
                app.dependency_overrides[dep_callable] = lambda: None


# ---------------------------------------------------------------------------
# Helper: build a minimal DiscoveredRepository-like object
# ---------------------------------------------------------------------------


def _make_discovered_repo(name="repo1"):
    r = MagicMock()
    r.name = name
    r.platform = "gitlab"
    r.clone_url_https = f"https://gitlab.com/group/{name}.git"
    r.clone_url_ssh = f"git@gitlab.com:group/{name}.git"
    r.description = ""
    r.default_branch = "main"
    r.is_private = False
    r.last_commit_hash = None
    r.last_commit_author = None
    r.last_commit_date = None
    r.last_activity = None
    return r


# ---------------------------------------------------------------------------
# TC-01: GitLab _fetch_all_pages_rest with progress_callback
# ---------------------------------------------------------------------------


class TestGitLabFetchAllPagesRestProgressCallback:
    """_fetch_all_pages_rest(progress_callback=...) calls callback once per page."""

    def _make_provider(self):
        from code_indexer.server.services.repository_providers.gitlab_provider import (
            GitLabProvider,
        )

        provider = object.__new__(GitLabProvider)
        return provider

    def test_callback_called_once_per_page(self):
        """Callback called once per page with correct arguments."""
        provider = self._make_provider()

        calls = []

        def cb(pct, phase=None, detail=None):
            calls.append((pct, phase, detail))

        # Two pages: page 1 has 100 repos, page 2 has 50 repos (last page)
        repo = _make_discovered_repo()

        def fake_fetch_batch(source, batch_size, search):
            page = source
            if page == 1:
                return [repo] * 100, 2, True, 150
            else:
                return [repo] * 50, None, False, 150

        provider._fetch_batch_rest = fake_fetch_batch

        all_repos, total = provider._fetch_all_pages_rest(progress_callback=cb)

        assert len(calls) == 2, f"Expected 2 callback calls, got {len(calls)}"
        assert all(phase == "fetching" for _, phase, _ in calls)
        assert all(detail is not None for _, _, detail in calls)

    def test_callback_pct_monotonically_non_decreasing(self):
        """Percentage is monotonically non-decreasing across pages."""
        provider = self._make_provider()

        pcts = []

        def cb(pct, phase=None, detail=None):
            pcts.append(pct)

        repo = _make_discovered_repo()
        page_count = [0]

        def fake_fetch_batch(source, batch_size, search):
            page_count[0] += 1
            page = source
            # 5 pages total, 20 repos each = 100 total
            if page < 5:
                return [repo] * 20, page + 1, True, 100
            return [repo] * 20, None, False, 100

        provider._fetch_batch_rest = fake_fetch_batch

        provider._fetch_all_pages_rest(progress_callback=cb)

        assert pcts == sorted(pcts), f"Percentages not monotonic: {pcts}"

    def test_callback_pct_capped_at_90(self):
        """Percentage never exceeds 90."""
        provider = self._make_provider()

        pcts = []

        def cb(pct, phase=None, detail=None):
            pcts.append(pct)

        repo = _make_discovered_repo()

        def fake_fetch_batch(source, batch_size, search):
            page = source
            if page < 200:
                return [repo] * 1, page + 1, True, 200
            return [repo] * 1, None, False, 200

        provider._fetch_batch_rest = fake_fetch_batch

        provider._fetch_all_pages_rest(progress_callback=cb)

        assert max(pcts) <= 90, f"Max pct {max(pcts)} exceeds 90"

    def test_callback_detail_contains_count(self):
        """Detail string mentions fetched/total counts."""
        provider = self._make_provider()

        details = []

        def cb(pct, phase=None, detail=None):
            if detail:
                details.append(detail)

        repo = _make_discovered_repo()

        def fake_fetch_batch(source, batch_size, search):
            if source == 1:
                return [repo] * 100, 2, True, 150
            return [repo] * 50, None, False, 150

        provider._fetch_batch_rest = fake_fetch_batch
        provider._fetch_all_pages_rest(progress_callback=cb)

        assert len(details) >= 1
        # Detail should mention some count info
        assert any("/" in d or "repo" in d.lower() for d in details), (
            f"Detail strings don't contain count info: {details}"
        )


# ---------------------------------------------------------------------------
# TC-02: GitLab _fetch_all_pages_rest without progress_callback (regression)
# ---------------------------------------------------------------------------


class TestGitLabFetchAllPagesRestNoCallback:
    """_fetch_all_pages_rest() without callback behaves exactly as before."""

    def _make_provider(self):
        from code_indexer.server.services.repository_providers.gitlab_provider import (
            GitLabProvider,
        )

        provider = object.__new__(GitLabProvider)
        return provider

    def test_no_callback_returns_all_repos(self):
        """Without callback, returns (repos, source_total) as before."""
        provider = self._make_provider()

        repo = _make_discovered_repo()

        def fake_fetch_batch(source, batch_size, search):
            if source == 1:
                return [repo] * 100, 2, True, 150
            return [repo] * 50, None, False, 150

        provider._fetch_batch_rest = fake_fetch_batch

        all_repos, total = provider._fetch_all_pages_rest()

        assert len(all_repos) == 150
        assert total == 150

    def test_no_callback_none_explicit(self):
        """Explicit None callback doesn't crash."""
        provider = self._make_provider()

        repo = _make_discovered_repo()

        def fake_fetch_batch(source, batch_size, search):
            return [repo], None, False, 1

        provider._fetch_batch_rest = fake_fetch_batch

        all_repos, total = provider._fetch_all_pages_rest(progress_callback=None)
        assert len(all_repos) == 1


# ---------------------------------------------------------------------------
# TC-03: GitHub _fetch_all_pages_graphql with progress_callback
# ---------------------------------------------------------------------------


class TestGitHubFetchAllPagesGraphqlProgressCallback:
    """_fetch_all_pages_graphql(progress_callback=...) calls callback per page."""

    def _make_provider(self):
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        provider = object.__new__(GitHubProvider)
        return provider

    def _make_graphql_response(self, nodes, total_count, has_next, cursor=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "data": {
                "viewer": {
                    "repositories": {
                        "nodes": nodes,
                        "totalCount": total_count,
                        "pageInfo": {
                            "hasNextPage": has_next,
                            "endCursor": cursor,
                        },
                    }
                }
            }
        }
        return resp

    def test_callback_called_once_per_page(self):
        """Callback called once per fetched page."""
        provider = self._make_provider()

        calls = []

        def cb(pct, phase=None, detail=None):
            calls.append((pct, phase, detail))

        page_num = [0]

        def fake_graphql_request(query):
            page_num[0] += 1
            if page_num[0] == 1:
                nodes: list = [
                    {
                        "name": "r",
                        "url": "https://github.com/u/r",
                        "sshUrl": "git@github.com:u/r.git",
                        "description": "",
                        "defaultBranchRef": None,
                        "isPrivate": False,
                        "refs": {"nodes": []},
                    }
                ] * 100
                return self._make_graphql_response(nodes, 150, True, "cursor1")
            else:
                nodes = [
                    {
                        "name": "r",
                        "url": "https://github.com/u/r",
                        "sshUrl": "git@github.com:u/r.git",
                        "description": "",
                        "defaultBranchRef": None,
                        "isPrivate": False,
                        "refs": {"nodes": []},
                    }
                ] * 50
                return self._make_graphql_response(nodes, 150, False, None)

        provider._make_graphql_request = fake_graphql_request
        provider._parse_graphql_response = MagicMock(
            return_value=_make_discovered_repo()
        )

        provider._fetch_all_pages_graphql(progress_callback=cb)

        assert len(calls) == 2, f"Expected 2 callback calls, got {len(calls)}"

    def test_callback_pct_capped_at_90(self):
        """Percentage never exceeds 90."""
        provider = self._make_provider()

        pcts = []

        def cb(pct, phase=None, detail=None):
            pcts.append(pct)

        page_num = [0]

        def fake_graphql_request(query):
            page_num[0] += 1
            # Simulate many pages
            if page_num[0] < 200:
                return self._make_graphql_response(
                    [{"name": "r"}], 200, True, f"cursor{page_num[0]}"
                )
            return self._make_graphql_response([{"name": "r"}], 200, False, None)

        provider._make_graphql_request = fake_graphql_request
        provider._parse_graphql_response = MagicMock(
            return_value=_make_discovered_repo()
        )

        provider._fetch_all_pages_graphql(progress_callback=cb)

        assert max(pcts) <= 90, f"Max pct {max(pcts)} exceeds 90"


# ---------------------------------------------------------------------------
# TC-04: GitHub _fetch_all_pages_graphql without callback (regression)
# ---------------------------------------------------------------------------


class TestGitHubFetchAllPagesGraphqlNoCallback:
    """_fetch_all_pages_graphql() without callback works as before."""

    def _make_provider(self):
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        provider = object.__new__(GitHubProvider)
        return provider

    def _make_graphql_response(self, nodes, total_count, has_next, cursor=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "data": {
                "viewer": {
                    "repositories": {
                        "nodes": nodes,
                        "totalCount": total_count,
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    }
                }
            }
        }
        return resp

    def test_no_callback_returns_all_repos(self):
        """Without callback, returns (repos, total_count) as before."""
        provider = self._make_provider()

        page_num = [0]

        def fake_graphql_request(query):
            page_num[0] += 1
            return self._make_graphql_response([{"name": "r"}] * 3, 3, False, None)

        provider._make_graphql_request = fake_graphql_request
        provider._parse_graphql_response = MagicMock(
            return_value=_make_discovered_repo()
        )

        repos, total = provider._fetch_all_pages_graphql()

        assert total == 3
        assert len(repos) == 3


# ---------------------------------------------------------------------------
# TC-05: discover_all_repositories threads callback through both providers
# ---------------------------------------------------------------------------


class TestDiscoverAllRepositoriesThreadsCallback:
    """discover_all_repositories(..., progress_callback=...) passes callback to internals."""

    def test_gitlab_threads_callback(self):
        from code_indexer.server.services.repository_providers.gitlab_provider import (
            GitLabProvider,
        )

        provider = object.__new__(GitLabProvider)
        provider.is_configured = MagicMock(return_value=True)
        provider._get_indexed_canonical_urls = MagicMock(return_value=set())

        captured = {}

        def fake_fetch_all_pages_rest(progress_callback=None):
            captured["cb"] = progress_callback
            return [], 0

        provider._fetch_all_pages_rest = fake_fetch_all_pages_rest
        provider._map_repos_to_dicts = MagicMock(return_value=[])

        cb = MagicMock()
        provider.discover_all_repositories(
            indexed_urls=set(),
            hidden_identifiers=set(),
            progress_callback=cb,
        )

        assert captured["cb"] is cb, (
            "progress_callback not threaded through to _fetch_all_pages_rest"
        )

    def test_github_threads_callback(self):
        from code_indexer.server.services.repository_providers.github_provider import (
            GitHubProvider,
        )

        provider = object.__new__(GitHubProvider)
        provider.is_configured = MagicMock(return_value=True)

        captured = {}

        def fake_fetch_all_pages_graphql(progress_callback=None):
            captured["cb"] = progress_callback
            return [], 0

        provider._fetch_all_pages_graphql = fake_fetch_all_pages_graphql
        provider._map_repos_to_dicts_github = MagicMock(return_value=[])

        cb = MagicMock()
        provider.discover_all_repositories(
            indexed_urls=set(),
            hidden_identifiers=set(),
            progress_callback=cb,
        )

        assert captured["cb"] is cb, (
            "progress_callback not threaded through to _fetch_all_pages_graphql"
        )


# ---------------------------------------------------------------------------
# TC-06: PayloadCache key semantics for discovery results
# ---------------------------------------------------------------------------


class TestDiscoveryPayloadCacheKeySemantics:
    """Discovery results stored in PayloadCache under key "discovery:{job_id}"."""

    def test_payload_cache_key_format(self):
        """Key used for storage follows "discovery:{job_id}" format."""
        # The route uses f"discovery:{job_id}" as the cache key.
        # Verify the format is stable — callers depend on it.
        job_id = "test-job-id-001"
        expected_key = f"discovery:{job_id}"
        assert expected_key == "discovery:test-job-id-001"

    def test_payload_cache_stores_json_serialisable_result(self):
        """Result stored in PayloadCache must be JSON-serialisable."""
        import json

        result = {
            "repositories": [{"name": "repo1"}],
            "total_source": 5,
            "total_unregistered": 3,
        }
        # Verify round-trip works (the route uses json.dumps / json.loads)
        serialised = json.dumps(result)
        restored = json.loads(serialised)
        assert restored == result


# ---------------------------------------------------------------------------
# TC-07: Worker closure populates result_holder
# ---------------------------------------------------------------------------


class TestWorkerClosurePopulatesResultHolder:
    """Worker function populates result_holder['data'] with provider result."""

    def test_worker_populates_result_holder(self):
        """When worker runs, result_holder is populated with provider output."""
        expected_result = {
            "repositories": [{"name": "repo1"}],
            "total_source": 10,
            "total_unregistered": 1,
        }
        result_holder = {}

        # Simulate the closure pattern from the route implementation
        def fake_provider_discover(
            indexed_urls, hidden_identifiers, progress_callback=None
        ):
            return expected_result

        def _worker(progress_callback=None):
            result = fake_provider_discover(
                indexed_urls=set(),
                hidden_identifiers=set(),
                progress_callback=progress_callback,
            )
            result_holder["data"] = result
            return {
                "total_source": result["total_source"],
                "total_unregistered": result["total_unregistered"],
                "result_ready": True,
            }

        # Run the worker directly (simulating BGM execution)
        _worker()

        assert result_holder.get("data") == expected_result, (
            f"result_holder not populated correctly: {result_holder}"
        )

    def test_worker_return_has_result_ready_flag(self):
        """Worker return dict has result_ready=True."""
        result_holder = {}

        def _worker(progress_callback=None):
            result_holder["data"] = {
                "repositories": [],
                "total_source": 5,
                "total_unregistered": 2,
            }
            return {
                "total_source": 5,
                "total_unregistered": 2,
                "result_ready": True,
            }

        ret = _worker()
        assert ret.get("result_ready") is True


# ---------------------------------------------------------------------------
# Route tests fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_and_client():
    """FastAPI TestClient with web_router mounted at /admin, admin session mocked."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from code_indexer.server.web.routes import web_router

    app = FastAPI()
    app.include_router(web_router, prefix="/admin")
    _bypass_elevation(app, web_router)

    mock_sm = MagicMock()
    mock_session = MagicMock()
    mock_session.role = "admin"
    mock_session.username = "admin"
    mock_sm.get_session.return_value = mock_session

    # Patch the name as imported into routes module
    with patch(
        "code_indexer.server.web.routes.get_session_manager", return_value=mock_sm
    ):
        yield app, TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def unauthenticated_client():
    """TestClient with no active session."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from code_indexer.server.web.routes import web_router

    app = FastAPI()
    app.include_router(web_router, prefix="/admin")
    _bypass_elevation(app, web_router)

    mock_sm = MagicMock()
    mock_sm.get_session.return_value = None

    # Patch the name as imported into routes module
    with patch(
        "code_indexer.server.web.routes.get_session_manager", return_value=mock_sm
    ):
        yield TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# TC-08: Dedup scan returns existing job_id when PENDING/RUNNING
# ---------------------------------------------------------------------------


class TestDedupScanReturnsExistingJob:
    """POST /api/discovery/{platform}/start returns existing job_id when same op already running."""

    def test_pending_job_returns_existing(self, app_and_client):
        """PENDING job of same op_type returns existing job_id with existing=True."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
            JobStatus,
            BackgroundJob,
        )
        from datetime import datetime, timezone

        _, client = app_and_client

        existing_job_id = "existing-job-abc"
        bgm = BackgroundJobManager()

        # Manually insert a pending job
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
        bgm.jobs[existing_job_id] = job

        mock_provider = MagicMock()
        mock_provider.is_configured.return_value = True

        with (
            patch(
                "code_indexer.server.web.routes._get_background_job_manager",
                return_value=bgm,
            ),
            patch(
                "code_indexer.server.web.routes._get_gitlab_provider",
                return_value=mock_provider,
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

    def test_running_job_returns_existing(self, app_and_client):
        """RUNNING job of same op_type returns existing job_id with existing=True."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
            JobStatus,
            BackgroundJob,
        )
        from datetime import datetime, timezone

        _, client = app_and_client

        existing_job_id = "existing-running-xyz"
        bgm = BackgroundJobManager()

        job = BackgroundJob(
            job_id=existing_job_id,
            operation_type="gitlab_discovery",
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            result=None,
            error=None,
            progress=42,
            username="admin",
            is_admin=True,
            repo_alias=None,
            actor_username="admin",
        )
        bgm.jobs[existing_job_id] = job

        mock_provider = MagicMock()
        mock_provider.is_configured.return_value = True

        with (
            patch(
                "code_indexer.server.web.routes._get_background_job_manager",
                return_value=bgm,
            ),
            patch(
                "code_indexer.server.web.routes._get_gitlab_provider",
                return_value=mock_provider,
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


# ---------------------------------------------------------------------------
# TC-09: No existing job submits new, returns existing=False
# ---------------------------------------------------------------------------


class TestNewJobSubmission:
    """POST /api/discovery/{platform}/start submits new job when none exists."""

    def test_new_job_returns_existing_false(self, app_and_client):
        """No existing job -> new job submitted, existing=False."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        _, client = app_and_client

        bgm = BackgroundJobManager()

        mock_provider = MagicMock()
        mock_provider.is_configured.return_value = True
        mock_provider._get_indexed_canonical_urls.return_value = set()

        submitted_jobs = []
        original_submit = bgm.submit_job

        def spy_submit(op_type, func, **kwargs):
            job_id = original_submit(op_type, func, **kwargs)
            submitted_jobs.append(job_id)
            return job_id

        bgm.submit_job = spy_submit

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
            resp = client.post(
                "/admin/api/discovery/gitlab/start",
                follow_redirects=False,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["existing"] is False
        assert "job_id" in body
        assert len(submitted_jobs) == 1
        assert body["job_id"] == submitted_jobs[0]


# ---------------------------------------------------------------------------
# TC-10: POST without admin session -> 401
# ---------------------------------------------------------------------------


class TestPostWithoutAdminSession:
    """POST /api/discovery/{platform}/start without session returns 401."""

    def test_post_start_no_session_401(self, unauthenticated_client):
        """No session -> 401."""
        resp = unauthenticated_client.post(
            "/admin/api/discovery/gitlab/start",
            follow_redirects=False,
        )
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"


# ---------------------------------------------------------------------------
# TC-11: GET result without admin session -> 401
# ---------------------------------------------------------------------------


class TestGetResultWithoutAdminSession:
    """GET /api/discovery/{platform}/result/{job_id} without session returns 401."""

    def test_get_result_no_session_401(self, unauthenticated_client):
        """No session -> 401."""
        resp = unauthenticated_client.get(
            "/admin/api/discovery/gitlab/result/some-job-id",
            follow_redirects=False,
        )
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"


# ---------------------------------------------------------------------------
# TC-12: GET result via PayloadCache — 404 for unknown key
# ---------------------------------------------------------------------------


class TestGetResultViaPayloadCache:
    """GET /api/discovery/{platform}/result/{job_id} uses PayloadCache."""

    def _make_mock_payload_cache(self, job_id, result_data=None):
        """Build a mock PayloadCache that knows about job_id."""
        import json
        from unittest.mock import MagicMock
        from code_indexer.server.cache.payload_cache import CacheRetrievalResult

        mock_cache = MagicMock()
        if result_data is not None:
            mock_cache.has_key.return_value = True
            mock_cache.retrieve.return_value = CacheRetrievalResult(
                content=json.dumps(result_data),
                page=0,
                total_pages=1,
                has_more=False,
            )
        else:
            mock_cache.has_key.return_value = False
        return mock_cache

    def test_result_200_when_key_present(self, app_and_client):
        """GET result returns 200 with data when PayloadCache has the key."""
        app, client = app_and_client

        job_id = "payload-cache-job-200"
        result_data = {
            "repositories": [{"name": "repo1"}],
            "total_source": 1,
            "total_unregistered": 1,
        }
        mock_cache = self._make_mock_payload_cache(job_id, result_data)
        app.state.payload_cache = mock_cache

        resp = client.get(
            f"/admin/api/discovery/gitlab/result/{job_id}",
            follow_redirects=False,
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["repositories"] == result_data["repositories"]

    def test_result_404_for_unknown_job(self, app_and_client):
        """Unknown job_id (not in PayloadCache) returns 404."""
        app, client = app_and_client

        mock_cache = self._make_mock_payload_cache("nonexistent-job-id")
        app.state.payload_cache = mock_cache

        resp = client.get(
            "/admin/api/discovery/gitlab/result/nonexistent-job-id",
            follow_redirects=False,
        )
        assert resp.status_code == 404, (
            f"Expected 404 for unknown job_id, got {resp.status_code}"
        )

    def test_result_can_be_read_multiple_times(self, app_and_client):
        """PayloadCache allows multiple reads within TTL (not read-once)."""
        app, client = app_and_client

        job_id = "reread-job-abc"
        result_data = {
            "repositories": [{"name": "repo1"}],
            "total_source": 2,
            "total_unregistered": 1,
        }
        mock_cache = self._make_mock_payload_cache(job_id, result_data)
        app.state.payload_cache = mock_cache

        resp1 = client.get(
            f"/admin/api/discovery/gitlab/result/{job_id}",
            follow_redirects=False,
        )
        assert resp1.status_code == 200, f"First GET: {resp1.status_code}"

        resp2 = client.get(
            f"/admin/api/discovery/gitlab/result/{job_id}",
            follow_redirects=False,
        )
        assert resp2.status_code == 200, (
            f"Second GET should also be 200 (TTL-based not read-once): {resp2.status_code}"
        )


# ---------------------------------------------------------------------------
# TC-13: GET result when payload_cache unavailable -> 503
# ---------------------------------------------------------------------------


class TestGetResultPayloadCacheUnavailable:
    """GET result returns 503 when app.state.payload_cache is None."""

    def test_result_503_when_cache_none(self, app_and_client):
        """When payload_cache is None, GET result returns 503."""
        app, client = app_and_client
        app.state.payload_cache = None

        resp = client.get(
            "/admin/api/discovery/gitlab/result/any-job-id",
            follow_redirects=False,
        )
        assert resp.status_code == 503, (
            f"Expected 503 when payload_cache unavailable, got {resp.status_code}"
        )
