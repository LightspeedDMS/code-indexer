"""Phase 3 E2E: Story #1400 async-hybrid temporal live wiring, front door only.

Real end-to-end proof that the async-hybrid machinery (worker, dedup cache,
poll, handoff) is actually REACHABLE from a real client -- not merely built
and unit-tested in isolation. Reuses the SAME real pre-built dual-embedder
temporal-indexed fixture repo test_18 uses (real VoyageAI embeddings, real
HNSW/temporal shard data, ~2860 real commits) via the identical REST-front-
door registration/activation pattern, so no new expensive indexing pass is
needed.

Scenario 11's exact lever: temporal_inline_wait_seconds is set to 0.001 via
the real ConfigService (in-process -- the TestClient and this test run in
the SAME process/interpreter, so this is a live, real config mutation, not a
mock) to deterministically force every temporal query down the async-handoff
path without needing a slow repo or artificial delays.

Covers (front door only, no mocks, real threads via BackgroundJobManager):
  - Scenario 2/3: MCP search_code on a forced-deferred temporal query
    returns success=False, job_id, partial_results, continue_polling=True,
    error_code=TEMPORAL_QUERY_DEFERRED -- the standard failure envelope
    plus additive fields.
  - Scenario 4: poll_search_job eventually resolves the SAME job to
    status="completed" with real results.
  - Scenario 12: the identical query issued via REST POST /api/query gets
    an equivalent HTTP 202 handoff, and GET /api/query/result/{job_id}
    eventually resolves it too.
  - Scenario 8: a non-temporal query is completely unaffected.

Requires ~/.tmp/temporal_recall_full_repo to exist (same fixture as
test_18_temporal_dual_embedder_1292.py) -- skips loudly if absent.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.e2e.server.conftest import AdminTokenProvider
from tests.e2e.server.mcp_helpers import call_mcp_tool, parse_mcp_result

_PREBUILT_REPO = Path.home() / ".tmp" / "temporal_recall_full_repo"
_ALIAS = "temporal-live-wiring-1400"
_JOB_TIMEOUT = 900.0
_JOB_POLL = 0.5
_POLL_SEARCH_JOB_TIMEOUT = 120.0
_POLL_SEARCH_JOB_INTERVAL = 1.0


def _wait_for_job(client: TestClient, job_id: str, headers: dict, label: str) -> None:
    deadline = time.monotonic() + _JOB_TIMEOUT
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}", headers=headers)
        assert resp.status_code < 500, (
            f"{label}: job poll HTTP {resp.status_code}: {resp.text[:200]}"
        )
        if resp.status_code == 200:
            body = resp.json()
            status = body.get("status")
            if status in ("completed", "failed", "cancelled"):
                assert status == "completed", f"{label}: job {job_id} -> {body}"
                return
        time.sleep(_JOB_POLL)
    raise TimeoutError(f"{label}: job {job_id} did not complete in {_JOB_TIMEOUT}s")


@pytest.fixture(scope="module")
def live_wiring_repo(
    test_client: TestClient,
    test_client_data_dir: Path,
    admin_token_provider: AdminTokenProvider,
) -> Iterator[str]:
    """Register + seed + activate the same real pre-built temporal repo
    test_18 uses -- identical pattern, separate alias to avoid collision."""
    if not _PREBUILT_REPO.exists():
        pytest.skip(
            f"Pre-built dual-embedder temporal repo not found at "
            f"{_PREBUILT_REPO} -- run the Story #1292 recall-gate setup first."
        )

    import json
    import shutil

    headers = admin_token_provider.get_headers()

    reg_resp = test_client.post(
        "/api/admin/golden-repos",
        json={"repo_url": str(_PREBUILT_REPO), "alias": _ALIAS},
        headers=headers,
    )
    assert reg_resp.status_code in (200, 202), (
        f"register HTTP {reg_resp.status_code}: {reg_resp.text[:300]}"
    )
    reg_job_id = reg_resp.json().get("job_id", "")
    assert reg_job_id
    _wait_for_job(
        test_client, reg_job_id, admin_token_provider.get_headers(), "register"
    )

    golden_repo_dir = test_client_data_dir / "data" / "golden-repos" / _ALIAS
    assert golden_repo_dir.exists(), f"golden repo clone missing at {golden_repo_dir}"

    src_index_dir = _PREBUILT_REPO / ".code-indexer" / "index"
    dst_index_dir = golden_repo_dir / ".code-indexer" / "index"
    for shard_dir in src_index_dir.glob("code-indexer-temporal-*"):
        shutil.copytree(shard_dir, dst_index_dir / shard_dir.name, dirs_exist_ok=True)

    config_path = golden_repo_dir / ".code-indexer" / "config.json"
    src_config = json.loads(
        (_PREBUILT_REPO / ".code-indexer" / "config.json").read_text()
    )
    dst_config = json.loads(config_path.read_text())
    dst_config["temporal"] = src_config["temporal"]
    config_path.write_text(json.dumps(dst_config, indent=2))

    act_resp = test_client.post(
        "/api/repos/activate",
        json={"golden_repo_alias": _ALIAS},
        headers=admin_token_provider.get_headers(),
    )
    assert act_resp.status_code in (200, 202), (
        f"activate HTTP {act_resp.status_code}: {act_resp.text[:300]}"
    )
    act_job_id = act_resp.json().get("job_id", "")
    assert act_job_id
    _wait_for_job(
        test_client, act_job_id, admin_token_provider.get_headers(), "activate"
    )

    yield _ALIAS


@pytest.fixture
def forced_deferred_inline_wait() -> Iterator[None]:
    """Scenario 11: deterministically force the async-handoff path by
    setting temporal_inline_wait_seconds to 0.001s via the REAL
    ConfigService (in-process -- same interpreter as the TestClient), then
    restore the original value afterward so other tests in this module
    (and any other module sharing this process) are unaffected."""
    from code_indexer.server.services.config_service import get_config_service

    config_service = get_config_service()
    original = (
        config_service.get_config().search_timeouts_config.temporal_inline_wait_seconds
    )
    config_service.update_setting(
        "search_timeouts", "temporal_inline_wait_seconds", 0.001
    )
    try:
        yield
    finally:
        config_service.update_setting(
            "search_timeouts", "temporal_inline_wait_seconds", original
        )


def _poll_search_job_until_completed(
    test_client: TestClient, job_id: str, headers: dict
) -> dict:
    deadline = time.monotonic() + _POLL_SEARCH_JOB_TIMEOUT
    last_body: dict = {}
    while time.monotonic() < deadline:
        resp = call_mcp_tool(
            test_client, "poll_search_job", {"job_id": job_id}, headers
        )
        assert resp.status_code == 200, (
            f"poll_search_job HTTP {resp.status_code}: {resp.text[:300]}"
        )
        last_body = parse_mcp_result(resp.json())
        if last_body.get("status") == "completed":
            return last_body
        assert last_body.get("status") != "failed", (
            f"poll_search_job reported failed: {last_body}"
        )
        time.sleep(_POLL_SEARCH_JOB_INTERVAL)
    raise TimeoutError(
        f"poll_search_job for {job_id} did not complete in "
        f"{_POLL_SEARCH_JOB_TIMEOUT}s; last body: {last_body}"
    )


class TestMcpForcedHandoffAndPoll:
    def test_search_code_forced_deferred_returns_handoff_envelope(
        self,
        test_client: TestClient,
        live_wiring_repo: str,
        auth_headers: dict,
        forced_deferred_inline_wait: None,
    ) -> None:
        """Scenario 2/3: with the inline wait forced to 0.001s, a real
        temporal MCP search_code call must degrade to the async-handoff
        envelope -- success=False, a real job_id, partial_results,
        continue_polling=True, error_code=TEMPORAL_QUERY_DEFERRED."""
        resp = call_mcp_tool(
            test_client,
            "search_code",
            {
                "repository_alias": live_wiring_repo,
                "query_text": "temporal query async hybrid execution",
                "time_range_all": True,
                "limit": 5,
            },
            auth_headers,
        )
        assert resp.status_code == 200, (
            f"search_code HTTP {resp.status_code}: {resp.text[:300]}"
        )
        body = parse_mcp_result(resp.json())

        assert body.get("success") is False, f"expected handoff, got: {body}"
        assert body.get("job_id"), f"handoff envelope missing job_id: {body}"
        assert body.get("continue_polling") is True
        assert body.get("error_code") == "TEMPORAL_QUERY_DEFERRED"
        assert "partial_results" in body

    def test_poll_search_job_eventually_resolves_the_deferred_job(
        self,
        test_client: TestClient,
        live_wiring_repo: str,
        auth_headers: dict,
        forced_deferred_inline_wait: None,
    ) -> None:
        """Scenario 4: the background worker keeps running after the
        handoff; polling poll_search_job for the SAME job_id must
        eventually resolve to status=completed with real results."""
        submit_resp = call_mcp_tool(
            test_client,
            "search_code",
            {
                "repository_alias": live_wiring_repo,
                "query_text": "quarterly shard routing temporal indexer",
                "time_range_all": True,
                "limit": 5,
            },
            auth_headers,
        )
        assert submit_resp.status_code == 200
        submit_body = parse_mcp_result(submit_resp.json())
        job_id = submit_body.get("job_id")
        assert job_id, f"expected a job_id from the forced handoff: {submit_body}"

        completed = _poll_search_job_until_completed(test_client, job_id, auth_headers)

        assert completed["status"] == "completed"
        assert completed.get("continue_polling") is False
        assert isinstance(completed.get("results"), list)


class TestRestForcedHandoffAndPoll:
    def test_post_query_forced_deferred_returns_202_handoff(
        self,
        test_client: TestClient,
        live_wiring_repo: str,
        auth_headers: dict,
        forced_deferred_inline_wait: None,
    ) -> None:
        """Scenario 12: an equivalent logical query issued via REST also
        degrades to an async handoff -- HTTP 202, job_id, error_code.
        Uses DISTINCT query_text from the MCP handoff test above: the
        TemporalDedupCache is a real per-process singleton spanning this
        whole test module, so reusing the identical query_text would let
        this REST call join the MCP test's (by-then-completed)
        background job and correctly return the cached completed result
        (200) instead of a fresh handoff -- proof the dedup mechanism
        works, but not what this specific test wants to exercise."""
        resp = test_client.post(
            "/api/query",
            json={
                "query_text": "REST door temporal async hybrid execution proof",
                "repository_alias": live_wiring_repo,
                "time_range_all": True,
                "limit": 5,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202, (
            f"expected 202 handoff, got {resp.status_code}: {resp.text[:300]}"
        )
        body = resp.json()
        assert body.get("job_id")
        assert body.get("continue_polling") is True
        assert body.get("error_code") == "TEMPORAL_QUERY_DEFERRED"

    def test_get_query_result_eventually_resolves_the_deferred_job(
        self,
        test_client: TestClient,
        live_wiring_repo: str,
        auth_headers: dict,
        forced_deferred_inline_wait: None,
    ) -> None:
        """Scenario 4/12: GET /api/query/result/{job_id} eventually
        resolves the REST-submitted job to a completed result. Uses
        DISTINCT query_text from the MCP poll test (see dedup-collision
        note on the sibling test above)."""
        submit_resp = test_client.post(
            "/api/query",
            json={
                "query_text": "REST door quarterly shard routing indexer proof",
                "repository_alias": live_wiring_repo,
                "time_range_all": True,
                "limit": 5,
            },
            headers=auth_headers,
        )
        assert submit_resp.status_code == 202
        job_id = submit_resp.json().get("job_id")
        assert job_id

        deadline = time.monotonic() + _POLL_SEARCH_JOB_TIMEOUT
        last_body: dict = {}
        while time.monotonic() < deadline:
            resp = test_client.get(f"/api/query/result/{job_id}", headers=auth_headers)
            assert resp.status_code in (200, 404), (
                f"unexpected status {resp.status_code}: {resp.text[:300]}"
            )
            last_body = resp.json()
            if last_body.get("status") == "completed":
                break
            assert last_body.get("status") != "failed", (
                f"REST poll reported failed: {last_body}"
            )
            time.sleep(_POLL_SEARCH_JOB_INTERVAL)
        else:
            raise TimeoutError(
                f"GET /api/query/result/{job_id} did not complete in "
                f"{_POLL_SEARCH_JOB_TIMEOUT}s; last body: {last_body}"
            )

        assert last_body["status"] == "completed"
        assert isinstance(last_body.get("results"), list)


class TestNonTemporalQueryUnaffected:
    def test_non_temporal_query_completes_normally_without_handoff_fields(
        self,
        test_client: TestClient,
        live_wiring_repo: str,
        auth_headers: dict,
        forced_deferred_inline_wait: None,
    ) -> None:
        """Scenario 8: a semantic query with NO temporal params must never
        enter the async-handoff path, even while temporal_inline_wait_
        seconds is forced to 0.001s -- proves the interception is gated
        strictly on temporal-param presence."""
        resp = call_mcp_tool(
            test_client,
            "search_code",
            {
                "repository_alias": live_wiring_repo,
                "query_text": "authentication logic",
                "limit": 5,
            },
            auth_headers,
        )
        assert resp.status_code == 200
        body = parse_mcp_result(resp.json())

        assert "job_id" not in body
        assert "continue_polling" not in body
        assert "error_code" not in body


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
