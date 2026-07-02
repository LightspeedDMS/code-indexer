"""
Tests for the fault injection admin REST router — Story #746 Phase D.

Scenarios covered:
  1  — harness inactive: all 11 endpoints return 404 (parametrized)
  4  — harness active: GET /status returns enabled + docs_url
  5  — non-admin: all 11 endpoints return 403 (parametrized)
  6  — PUT / GET profile CRUD
  7  — DELETE single + DELETE all
  13 — PATCH partial update
  14 — POST /seed reproducible sequence
  16 — POST /reset clears everything
  20 — docs_url in status response
"""

from __future__ import annotations

import random
from typing import Any, Dict, Optional, Tuple
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
    select_outcome,
)
from code_indexer.server.fault_injection.fault_profile import FaultProfile
from code_indexer.server.fault_injection.router import (
    DOCS_URL,
    router,
    set_service_on_app_state,
)

# ---------------------------------------------------------------------------
# Module-level immutable constants
# ---------------------------------------------------------------------------

_TARGET: str = "api.voyageai.com"
_SEED: int = 42

# All 11 endpoints as a tuple-of-tuples: (http_method, path, json_body_or_None).
# Payloads in the tuple are None; fresh dicts are created in _call() when needed.
_ALL_ENDPOINTS: Tuple[Tuple[str, str, bool], ...] = (
    # (method, path, needs_body)
    ("GET", "/admin/fault-injection/status", False),
    ("GET", "/admin/fault-injection/profiles", False),
    ("GET", f"/admin/fault-injection/profiles/{_TARGET}", False),
    ("PUT", f"/admin/fault-injection/profiles/{_TARGET}", True),
    ("PATCH", f"/admin/fault-injection/profiles/{_TARGET}", True),
    ("DELETE", f"/admin/fault-injection/profiles/{_TARGET}", False),
    ("DELETE", "/admin/fault-injection/profiles", False),
    ("POST", "/admin/fault-injection/reset", False),
    ("POST", "/admin/fault-injection/preview", True),
    ("GET", "/admin/fault-injection/history", False),
    ("POST", "/admin/fault-injection/seed", True),
)


# ---------------------------------------------------------------------------
# Per-call payload factories (fresh dict per call — no shared mutable state)
# ---------------------------------------------------------------------------


def _profile_body(target: str = _TARGET) -> Dict[str, Any]:
    return {"target": target, "error_rate": 1.0, "error_codes": [429]}


def _body_for(method: str, path: str) -> Optional[Dict[str, Any]]:
    """Return a suitable fresh request body for parametrized endpoint tests."""
    if "PUT" in method and "profiles" in path:
        return _profile_body()
    if "PATCH" in method and "profiles" in path:
        return {"enabled": False}
    if "preview" in path:
        return {"url": f"https://{_TARGET}/v1/embeddings"}
    if "seed" in path:
        return {"seed": 42}
    return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_svc(enabled: bool = True) -> FaultInjectionService:
    return FaultInjectionService(enabled=enabled, rng=random.Random(_SEED))


def _make_app(svc: Optional[FaultInjectionService], admin: bool) -> FastAPI:
    from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid

    app = FastAPI()
    app.include_router(router)
    if svc is not None:
        set_service_on_app_state(app, svc)
    if admin:
        mock_admin = MagicMock()
        mock_admin.username = "admin"
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: mock_admin
    else:

        def _forbidden() -> None:
            raise HTTPException(status_code=403, detail="Forbidden")

        app.dependency_overrides[get_current_admin_user_hybrid] = _forbidden
    return app


def _call(client: TestClient, method: str, path: str, body: Optional[Dict]) -> Any:
    fn = getattr(client, method.lower())
    return fn(path, json=body) if body is not None else fn(path)


# ---------------------------------------------------------------------------
# Scenario 1 — harness inactive: ALL 11 endpoints return 404 (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,needs_body", _ALL_ENDPOINTS)
def test_all_endpoints_return_404_when_harness_inactive(
    method: str, path: str, needs_body: bool
) -> None:
    """Scenario 1: service absent from app.state → every endpoint returns 404."""
    c = TestClient(_make_app(svc=None, admin=True), raise_server_exceptions=False)
    body = _body_for(method, path) if needs_body else None
    r = _call(c, method, path, body)
    assert r.status_code == 404, f"{method} {path} expected 404, got {r.status_code}"


# ---------------------------------------------------------------------------
# Scenario 5 — non-admin: ALL 11 endpoints return 403 (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,needs_body", _ALL_ENDPOINTS)
def test_all_endpoints_return_403_for_non_admin(
    method: str, path: str, needs_body: bool
) -> None:
    """Scenario 5: non-admin auth → every endpoint returns 403."""
    c = TestClient(
        _make_app(svc=_make_svc(), admin=False), raise_server_exceptions=False
    )
    body = _body_for(method, path) if needs_body else None
    r = _call(c, method, path, body)
    assert r.status_code == 403, f"{method} {path} expected 403, got {r.status_code}"


# ---------------------------------------------------------------------------
# Fixtures for scenario test classes
# ---------------------------------------------------------------------------


@pytest.fixture()
def active_app() -> FastAPI:
    return _make_app(svc=_make_svc(), admin=True)


@pytest.fixture()
def cl(active_app: FastAPI) -> TestClient:
    return TestClient(active_app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Scenario 4 + 20 — GET /status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_returns_200_with_harness_active(self, cl: TestClient) -> None:
        r = cl.get("/admin/fault-injection/status")
        assert r.status_code == 200
        assert r.json()["enabled"] is True
        assert isinstance(r.json()["counters"], dict)

    def test_includes_docs_url(self, cl: TestClient) -> None:
        body = cl.get("/admin/fault-injection/status").json()
        assert body["docs_url"] == DOCS_URL
        assert "fault-injection-operator-guide" in body["docs_url"]

    def test_profile_count_increments_after_upsert(self, cl: TestClient) -> None:
        cl.put(f"/admin/fault-injection/profiles/{_TARGET}", json=_profile_body())
        assert cl.get("/admin/fault-injection/status").json()["profile_count"] == 1


# ---------------------------------------------------------------------------
# Scenario 6 — PUT / GET CRUD
# ---------------------------------------------------------------------------


class TestProfileCrud:
    def test_put_returns_created_profile(self, cl: TestClient) -> None:
        r = cl.put(f"/admin/fault-injection/profiles/{_TARGET}", json=_profile_body())
        assert r.status_code == 200
        assert r.json()["target"] == _TARGET
        assert r.json()["error_rate"] == 1.0

    def test_put_path_target_overrides_body_target(self, cl: TestClient) -> None:
        r = cl.put(
            f"/admin/fault-injection/profiles/{_TARGET}",
            json={**_profile_body(), "target": "wrong.host"},
        )
        assert r.json()["target"] == _TARGET

    def test_get_missing_profile_returns_404(self, cl: TestClient) -> None:
        assert cl.get("/admin/fault-injection/profiles/no.such.host").status_code == 404

    def test_list_profiles_reflects_upserted(self, cl: TestClient) -> None:
        cl.put(f"/admin/fault-injection/profiles/{_TARGET}", json=_profile_body())
        profiles = cl.get("/admin/fault-injection/profiles").json()["profiles"]
        assert len(profiles) == 1 and profiles[0]["target"] == _TARGET

    def test_put_with_invalid_rate_returns_400(self, cl: TestClient) -> None:
        r = cl.put(
            f"/admin/fault-injection/profiles/{_TARGET}",
            json={**_profile_body(), "error_rate": 2.0},
        )
        assert r.status_code == 400

    def test_put_with_bad_range_pair_returns_400(self, cl: TestClient) -> None:
        r = cl.put(
            f"/admin/fault-injection/profiles/{_TARGET}",
            json={**_profile_body(), "retry_after_sec_range": [1, 2, 3]},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Scenario 7 — DELETE endpoints
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_single_removes_profile(self, cl: TestClient) -> None:
        cl.put(f"/admin/fault-injection/profiles/{_TARGET}", json=_profile_body())
        r = cl.delete(f"/admin/fault-injection/profiles/{_TARGET}")
        assert r.status_code == 200
        assert cl.get(f"/admin/fault-injection/profiles/{_TARGET}").status_code == 404

    def test_delete_all_clears_registry(self, cl: TestClient) -> None:
        for host in (_TARGET, "api.cohere.com"):
            cl.put(f"/admin/fault-injection/profiles/{host}", json=_profile_body(host))
        r = cl.delete("/admin/fault-injection/profiles")
        assert r.json()["cleared"] == 2
        assert cl.get("/admin/fault-injection/profiles").json()["profiles"] == []


# ---------------------------------------------------------------------------
# Scenario 13 — PATCH partial update
# ---------------------------------------------------------------------------


class TestPatch:
    def test_patch_preserves_untouched_fields(self, cl: TestClient) -> None:
        cl.put(
            f"/admin/fault-injection/profiles/{_TARGET}",
            json={
                **_profile_body(),
                "error_rate": 0.5,
                "dns_failure_rate": 0.5,
            },
        )
        r = cl.patch(
            f"/admin/fault-injection/profiles/{_TARGET}", json={"enabled": False}
        )
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert r.json()["dns_failure_rate"] == 0.5
        assert r.json()["error_codes"] == [429]

    def test_patch_missing_profile_returns_404(self, cl: TestClient) -> None:
        assert (
            cl.patch(
                "/admin/fault-injection/profiles/no.host", json={"enabled": False}
            ).status_code
            == 404
        )

    def test_patch_bad_range_returns_400(self, cl: TestClient) -> None:
        cl.put(f"/admin/fault-injection/profiles/{_TARGET}", json=_profile_body())
        r = cl.patch(
            f"/admin/fault-injection/profiles/{_TARGET}",
            json={"retry_after_sec_range": [1, 2, 3]},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Scenario 16 — POST /reset clears everything
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_profiles_counters_history(
        self, active_app: FastAPI, cl: TestClient
    ) -> None:
        cl.put(f"/admin/fault-injection/profiles/{_TARGET}", json=_profile_body())
        active_app.state.fault_injection_service.record_injection(
            _TARGET, "http_error", "x"
        )
        assert cl.post("/admin/fault-injection/reset").json()["reset"] is True
        assert cl.get("/admin/fault-injection/profiles").json()["profiles"] == []
        assert cl.get("/admin/fault-injection/status").json()["counters"] == {}
        assert cl.get("/admin/fault-injection/history").json()["history"] == []


# ---------------------------------------------------------------------------
# POST /preview
# ---------------------------------------------------------------------------


class TestPreview:
    def test_no_match_returns_null(self, cl: TestClient) -> None:
        r = cl.post(
            "/admin/fault-injection/preview", json={"url": f"https://{_TARGET}/v1"}
        )
        assert r.json()["matched"] is None

    def test_match_returns_profile(self, cl: TestClient) -> None:
        cl.put(f"/admin/fault-injection/profiles/{_TARGET}", json=_profile_body())
        r = cl.post(
            "/admin/fault-injection/preview", json={"url": f"https://{_TARGET}/v1"}
        )
        assert r.json()["matched"]["error_rate"] == 1.0

    def test_url_without_hostname_returns_400(self, cl: TestClient) -> None:
        assert (
            cl.post(
                "/admin/fault-injection/preview", json={"url": "no-scheme"}
            ).status_code
            == 400
        )

    def test_does_not_record_injection_event(
        self, active_app: FastAPI, cl: TestClient
    ) -> None:
        cl.put(f"/admin/fault-injection/profiles/{_TARGET}", json=_profile_body())
        svc: FaultInjectionService = active_app.state.fault_injection_service
        before = len(svc.get_history())
        cl.post("/admin/fault-injection/preview", json={"url": f"https://{_TARGET}/v1"})
        assert len(svc.get_history()) == before


# ---------------------------------------------------------------------------
# Scenario 14 — POST /seed reproducibility
# ---------------------------------------------------------------------------


class TestSeed:
    def test_seed_returns_seeded_value(self, cl: TestClient) -> None:
        assert (
            cl.post("/admin/fault-injection/seed", json={"seed": 42}).json()["seeded"]
            == 42
        )

    def test_same_seed_produces_identical_sequence(
        self, active_app: FastAPI, cl: TestClient
    ) -> None:
        svc: FaultInjectionService = active_app.state.fault_injection_service
        svc.register_profile(
            _TARGET, FaultProfile(target=_TARGET, error_rate=0.5, error_codes=[429])
        )
        cl.post("/admin/fault-injection/seed", json={"seed": 42})
        seq_a = [select_outcome(svc.get_profile(_TARGET), svc.rng) for _ in range(20)]

        cl.post("/admin/fault-injection/seed", json={"seed": 42})
        seq_b = [select_outcome(svc.get_profile(_TARGET), svc.rng) for _ in range(20)]

        assert seq_a == seq_b


# ---------------------------------------------------------------------------
# Wiring: set_service_on_app_state
# ---------------------------------------------------------------------------


class TestWiring:
    def test_none_service_causes_404(self) -> None:
        c = TestClient(_make_app(svc=None, admin=True), raise_server_exceptions=False)
        assert c.get("/admin/fault-injection/status").status_code == 404

    def test_active_service_causes_200(self) -> None:
        c = TestClient(
            _make_app(svc=_make_svc(), admin=True), raise_server_exceptions=False
        )
        assert c.get("/admin/fault-injection/status").status_code == 200

    def test_double_wire_same_service_is_idempotent(self) -> None:
        """N3: Wiring the same service instance twice must succeed (idempotent)."""
        app = FastAPI()
        svc = _make_svc()
        set_service_on_app_state(app, svc)
        set_service_on_app_state(app, svc)
        assert app.state.fault_injection_service is svc

    def test_double_wire_different_service_raises(self) -> None:
        """N3: Wiring a different service instance over an already-wired one must
        raise AssertionError (double-wire guard).
        """
        app = FastAPI()
        svc_a = _make_svc()
        svc_b = _make_svc()
        set_service_on_app_state(app, svc_a)
        with pytest.raises(AssertionError, match="double-wire"):
            set_service_on_app_state(app, svc_b)


# ---------------------------------------------------------------------------
# Coverage gap tests — router.py lines 367-369, 388, 445
# ---------------------------------------------------------------------------


class TestGetProfileHappyPath:
    """Cover router.py line 445: GET /profiles/{target} with existing profile."""

    def test_get_existing_profile_returns_200(self, cl: TestClient) -> None:
        cl.put(f"/admin/fault-injection/profiles/{_TARGET}", json=_profile_body())
        r = cl.get(f"/admin/fault-injection/profiles/{_TARGET}")
        assert r.status_code == 200
        assert r.json()["target"] == _TARGET


class TestPreviewCoverageGaps:
    """Cover router.py lines 367-369 (ValueError) and 388 (no-match None)."""

    def test_preview_with_malformed_url_returns_400(self, cl: TestClient) -> None:
        """Cover lines 367-369: urlparse raises ValueError for truly malformed URL."""
        # A URL with an invalid IPv6 address triggers ValueError in urlparse.
        r = cl.post(
            "/admin/fault-injection/preview",
            json={"url": "http://[invalid-ipv6/path"},
        )
        assert r.status_code == 400

    def test_preview_no_registered_profile_returns_null(self, cl: TestClient) -> None:
        """Preview returns {matched: null} when no profile is registered for the URL."""
        r = cl.post(
            "/admin/fault-injection/preview",
            json={"url": "https://no.profile.registered.com/v1/embed"},
        )
        assert r.status_code == 200
        assert r.json()["matched"] is None

    def test_preview_wildcard_profile_returns_registered_target_key(
        self, cl: TestClient
    ) -> None:
        """M2: Preview with a wildcard profile must return the registered key
        (*.voyageai.com), not the hostname extracted from the URL (api.voyageai.com).
        """
        wildcard_target = "*.voyageai.com"
        put_response = cl.put(
            f"/admin/fault-injection/profiles/{wildcard_target}",
            json=_profile_body(wildcard_target),
        )
        assert put_response.status_code == 200, (
            f"Profile registration failed: {put_response.json()}"
        )
        r = cl.post(
            "/admin/fault-injection/preview",
            json={"url": "https://api.voyageai.com/v1/embeddings"},
        )
        assert r.status_code == 200
        matched = r.json()["matched"]
        assert matched is not None
        assert matched["target"] == wildcard_target, (
            f"Expected target key '{wildcard_target}', got '{matched['target']}'"
        )

    def test_put_profile_with_slash_in_target_is_rejected(self, cl: TestClient) -> None:
        """N2: Targets containing slashes must be rejected — hostnames never contain
        slashes and {target:path} was too permissive.

        With {target} (no path), FastAPI routes a slash-containing URL to 404
        because it does not match any registered route.  Both 400 and 404 are
        correct rejection outcomes; either is accepted here.
        """
        slash_target = "api.voyageai.com/v1/evil"
        r = cl.put(
            f"/admin/fault-injection/profiles/{slash_target}",
            json=_profile_body("api.voyageai.com"),
        )
        assert r.status_code in (
            400,
            404,
        ), f"Expected 400 or 404 for slash-containing target, got {r.status_code}"

    def test_put_profile_with_valid_wildcard_target_succeeds(
        self, cl: TestClient
    ) -> None:
        """N2: Wildcard hostname targets (*.host.com) must still be accepted."""
        wildcard_target = "*.voyageai.com"
        r = cl.put(
            f"/admin/fault-injection/profiles/{wildcard_target}",
            json=_profile_body(wildcard_target),
        )
        assert r.status_code == 200, (
            f"Expected 200 for wildcard target, got {r.status_code}: {r.json()}"
        )

    def test_preview_disabled_exact_and_enabled_wildcard_returns_wildcard_key(
        self, cl: TestClient
    ) -> None:
        """M2 edge case: disabled api.voyageai.com + enabled *.voyageai.com.

        Preview response target must come from the snapshot's own profile.target
        (*.voyageai.com), not a re-derived hostname key.  The disabled exact
        profile must not shadow the enabled wildcard profile.
        """
        exact_target = "api.voyageai.com"
        wildcard_target = "*.voyageai.com"

        disabled_body = {**_profile_body(exact_target), "enabled": False}
        r1 = cl.put(
            f"/admin/fault-injection/profiles/{exact_target}", json=disabled_body
        )
        assert r1.status_code == 200

        r2 = cl.put(
            f"/admin/fault-injection/profiles/{wildcard_target}",
            json=_profile_body(wildcard_target),
        )
        assert r2.status_code == 200

        r = cl.post(
            "/admin/fault-injection/preview",
            json={"url": f"https://{exact_target}/v1/embeddings"},
        )
        assert r.status_code == 200
        matched = r.json()["matched"]
        assert matched is not None, "Expected wildcard profile to match"
        assert matched["target"] == wildcard_target, (
            f"Expected wildcard key '{wildcard_target}', got '{matched['target']}'. "
            "Snapshot target must be used, not a re-derived hostname."
        )
