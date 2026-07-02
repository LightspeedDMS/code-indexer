"""
AC4: Fault profile CRUD against the live fault server.

Tests all five HTTP verbs (PUT, GET, PATCH, DELETE, POST /reset) against a
real uvicorn subprocess started by e2e-automation.sh --phase 5.  No mocking.

Also verifies admin-auth enforcement: at least one request sent without a
bearer token must return 401 or 403.

Depends on session fixtures from conftest.py:
  fault_http_client   -- httpx.Client bound to the fault server (unauthenticated)
  fault_admin_client  -- FaultAdminClient with re_login() support
  clear_all_faults    -- autouse, resets state before each test (AC5)

Target hostnames (api.voyageai.com, api.cohere.com) are fixed protocol values
required by the AC4 specification — they match the httpx transport-layer targets
the fault injection harness intercepts.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Fixed fault-injection target hostnames (AC4 specification requirement).
# These are the exact hostnames the fault harness intercepts at the httpx
# transport layer; they are not environment-specific configuration values.
# ---------------------------------------------------------------------------

VOYAGE_TARGET = "api.voyageai.com"
COHERE_TARGET = "api.cohere.com"

_KILL_VOYAGE = {
    "target": VOYAGE_TARGET,
    "enabled": True,
    "error_rate": 1.0,
    "error_codes": [503],
}

_KILL_COHERE = {
    "target": COHERE_TARGET,
    "enabled": True,
    "error_rate": 1.0,
    "error_codes": [503],
}


# ---------------------------------------------------------------------------
# AC4 — fault profile CRUD (part 1 of 2: PUT / GET / PATCH / DELETE)
# ---------------------------------------------------------------------------


class TestFaultProfileCRUD:
    """Exercise PUT / GET / PATCH / DELETE / POST reset against the live server."""

    def test_put_creates_profile_and_get_returns_it(
        self, fault_admin_client, clear_all_faults
    ):
        """PUT creates a kill profile; GET returns the same profile."""
        put_resp = fault_admin_client.put(
            f"/admin/fault-injection/profiles/{VOYAGE_TARGET}",
            json=_KILL_VOYAGE,
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT expected 2xx, got {put_resp.status_code}: {put_resp.text}"
        )

        get_resp = fault_admin_client.get(
            f"/admin/fault-injection/profiles/{VOYAGE_TARGET}"
        )
        assert get_resp.status_code == 200, (
            f"GET expected 200, got {get_resp.status_code}: {get_resp.text}"
        )
        body = get_resp.json()
        assert body["target"] == VOYAGE_TARGET
        assert body["error_rate"] == 1.0
        assert body["error_codes"] == [503]
        assert body["enabled"] is True

    def test_patch_merges_fields_into_existing_profile(
        self, fault_admin_client, clear_all_faults
    ):
        """PATCH updates only supplied fields; other fields are preserved."""
        put_resp = fault_admin_client.put(
            f"/admin/fault-injection/profiles/{VOYAGE_TARGET}",
            json=_KILL_VOYAGE,
        )
        assert put_resp.status_code in (200, 201)

        patch_resp = fault_admin_client.patch(
            f"/admin/fault-injection/profiles/{VOYAGE_TARGET}",
            json={"latency_rate": 0.5},
        )
        assert patch_resp.status_code == 200, (
            f"PATCH expected 200, got {patch_resp.status_code}: {patch_resp.text}"
        )

        get_resp = fault_admin_client.get(
            f"/admin/fault-injection/profiles/{VOYAGE_TARGET}"
        )
        assert get_resp.status_code == 200
        merged = get_resp.json()
        assert merged["error_rate"] == 1.0, "PATCH must preserve error_rate"
        assert merged["latency_rate"] == 0.5, "PATCH must apply latency_rate"

    def test_delete_removes_profile_and_get_returns_404(
        self, fault_admin_client, clear_all_faults
    ):
        """DELETE removes a profile; subsequent GET returns 404."""
        put_resp = fault_admin_client.put(
            f"/admin/fault-injection/profiles/{COHERE_TARGET}",
            json=_KILL_COHERE,
        )
        assert put_resp.status_code in (200, 201)

        del_resp = fault_admin_client.delete(
            f"/admin/fault-injection/profiles/{COHERE_TARGET}"
        )
        assert del_resp.status_code == 200, (
            f"DELETE expected 200, got {del_resp.status_code}: {del_resp.text}"
        )

        get_resp = fault_admin_client.get(
            f"/admin/fault-injection/profiles/{COHERE_TARGET}"
        )
        assert get_resp.status_code == 404, (
            f"GET after DELETE expected 404, got {get_resp.status_code}"
        )

    def test_reset_clears_all_profiles_and_history(
        self, fault_admin_client, clear_all_faults
    ):
        """POST /reset clears all profiles and history atomically."""
        for target, payload in [
            (VOYAGE_TARGET, _KILL_VOYAGE),
            (COHERE_TARGET, _KILL_COHERE),
        ]:
            resp = fault_admin_client.put(
                f"/admin/fault-injection/profiles/{target}", json=payload
            )
            assert resp.status_code in (200, 201)

        reset_resp = fault_admin_client.post("/admin/fault-injection/reset")
        assert reset_resp.status_code == 200, (
            f"POST /reset expected 200, got {reset_resp.status_code}: {reset_resp.text}"
        )

        for target in (VOYAGE_TARGET, COHERE_TARGET):
            get_resp = fault_admin_client.get(
                f"/admin/fault-injection/profiles/{target}"
            )
            assert get_resp.status_code == 404, (
                f"GET {target} after reset expected 404, got {get_resp.status_code}"
            )

        history_resp = fault_admin_client.get("/admin/fault-injection/history")
        assert history_resp.status_code == 200
        assert history_resp.json()["history"] == [], "History must be empty after reset"

    def test_unauthenticated_requests_rejected(
        self, fault_http_client, clear_all_faults
    ):
        """Requests without a bearer token must be rejected with 401 or 403."""
        resp = fault_http_client.get("/admin/fault-injection/status")
        assert resp.status_code in (401, 403), (
            f"Unauthenticated GET /status expected 401/403, got {resp.status_code}"
        )

        resp = fault_http_client.put(
            f"/admin/fault-injection/profiles/{VOYAGE_TARGET}",
            json=_KILL_VOYAGE,
        )
        assert resp.status_code in (401, 403), (
            f"Unauthenticated PUT expected 401/403, got {resp.status_code}"
        )

        resp = fault_http_client.post("/admin/fault-injection/reset")
        assert resp.status_code in (401, 403), (
            f"Unauthenticated POST /reset expected 401/403, got {resp.status_code}"
        )
