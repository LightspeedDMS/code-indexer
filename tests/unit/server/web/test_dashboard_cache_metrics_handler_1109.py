"""Story #1109 (S5) AC3: Dashboard cache-metrics partial handler — handler invocation tests.

Verifies two AC3 defects fixed in code review:

  Defect 1 (data-source): dashboard_cache_metrics_partial used
      getattr(request.app.state, "query_embedding_cache", None) which is
      NEVER set, so total_entries was always 0.  Fix: use _get_qec_total_entries()
      which calls get_query_embedding_cache() from governed_call.

  Defect 2 (orphan UI): no parent template fetched the
      /admin/partials/dashboard-cache-metrics route.  Fix: dashboard_stats.html
      now contains an HTMX hx-get trigger for the route.

Tests use FastAPI TestClient with form-based admin login (matching the pattern
in test_depmap_running_state_bugs.py) so the handler runs through the full
request cycle.  set_query_embedding_cache / set_query_embedding_cache_metrics
install fake objects into the process-global accessors; clear_* restores them
in teardown so other tests are not polluted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_DASHBOARD_STATS_TEMPLATE = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src/code_indexer/server/web/templates/partials/dashboard_stats.html"
)


# ---------------------------------------------------------------------------
# Fake objects
# ---------------------------------------------------------------------------


class _FakeCache:
    """Minimal fake that satisfies total_entries() with a known value."""

    def __init__(self, count: int = 42) -> None:
        self._count = count

    def total_entries(self) -> int:
        return self._count


class _FakeMetrics:
    """Minimal fake that satisfies snapshot() with known per-mode tallies."""

    def __init__(
        self,
        shadow_hits: int = 7,
        shadow_misses: int = 3,
        on_hits: int = 5,
        on_misses: int = 5,
        shadow_cosine_p50: float = 0.97,
    ) -> None:
        self._snap = {
            "shadow": {"hits": shadow_hits, "misses": shadow_misses},
            "on": {"hits": on_hits, "misses": on_misses},
            "shadow_cosine_p50": shadow_cosine_p50,
        }

    def snapshot(self) -> dict:
        return self._snap


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_depmap_running_state_bugs.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    from code_indexer.server.app import app as _app

    return _app


@pytest.fixture(scope="module")
def client(app):
    return TestClient(app)


@pytest.fixture(scope="module")
def admin_session_cookie(client):
    """Form-based login — identical to test_depmap_running_state_bugs.py."""
    login_page = client.get("/login")
    assert login_page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match, "Could not extract CSRF token from login page"
    csrf_token = match.group(1)

    login_resp = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert login_resp.status_code == 303, f"Form login failed: {login_resp.status_code}"
    assert "session" in login_resp.cookies, "No session cookie set by form login"
    for name, value in login_resp.cookies.items():
        client.cookies.set(name, value)
    return login_resp.cookies


# ---------------------------------------------------------------------------
# Defect 1: handler uses real cache data source (not hardcoded 0)
# ---------------------------------------------------------------------------


class TestHandlerUsesRealCacheDataSource:
    """Verify the handler calls get_query_embedding_cache() via _get_qec_total_entries()."""

    def test_handler_returns_200(self, client, admin_session_cookie):
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )

        set_query_embedding_cache(_FakeCache(count=42))
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
        finally:
            clear_query_embedding_cache()

    def test_handler_renders_real_total_entries(self, client, admin_session_cookie):
        """total_entries must come from the real cache accessor, not hardcoded 0."""
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )

        set_query_embedding_cache(_FakeCache(count=42))
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            assert "42" in resp.text, (
                f"Expected '42' in rendered HTML but got:\n{resp.text[:500]}"
            )
        finally:
            clear_query_embedding_cache()

    def test_handler_renders_zero_when_cache_absent(self, client, admin_session_cookie):
        """When cache is not wired (None), total_entries must render as 0 (fail-open)."""
        from code_indexer.server.services.governed_call import (
            clear_query_embedding_cache,
        )

        clear_query_embedding_cache()
        resp = client.get("/admin/partials/dashboard-cache-metrics")
        assert resp.status_code == 200
        # Should not blow up; should render 0 gracefully

    def test_handler_renders_per_mode_hits(self, client, admin_session_cookie):
        """Shadow and on-mode hits/misses must appear from the metrics snapshot."""
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
            set_query_embedding_cache_metrics,
            clear_query_embedding_cache_metrics,
        )

        fake_metrics = _FakeMetrics(
            shadow_hits=7,
            shadow_misses=3,
            on_hits=5,
            on_misses=5,
            shadow_cosine_p50=0.97,
        )
        set_query_embedding_cache(_FakeCache(count=42))
        set_query_embedding_cache_metrics(fake_metrics)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            # 42 total entries
            assert "42" in html, f"Expected '42' in HTML, got:\n{html[:500]}"
            # shadow hits = 7, shadow requests = 7+3 = 10
            assert "7" in html, "Expected shadow hits '7' in HTML"
            assert "10" in html, "Expected shadow requests '10' in HTML"
            # on hits = 5, on requests = 5+5 = 10
            assert "5" in html, "Expected on hits '5' in HTML"
        finally:
            clear_query_embedding_cache()
            clear_query_embedding_cache_metrics()

    def test_handler_renders_cosine_p50(self, client, admin_session_cookie):
        """Shadow cosine p50 must appear in the rendered HTML."""
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
            set_query_embedding_cache_metrics,
            clear_query_embedding_cache_metrics,
        )

        fake_metrics = _FakeMetrics(shadow_cosine_p50=0.97)
        set_query_embedding_cache(_FakeCache(count=42))
        set_query_embedding_cache_metrics(fake_metrics)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            # cosine p50 = 0.97 must appear somewhere in the HTML
            assert "0.97" in resp.text, (
                f"Expected cosine p50 '0.97' in HTML, got:\n{resp.text[:500]}"
            )
        finally:
            clear_query_embedding_cache()
            clear_query_embedding_cache_metrics()


# ---------------------------------------------------------------------------
# Defect 2: parent template now contains the HTMX trigger
# ---------------------------------------------------------------------------


class TestParentTemplateContainsCacheMetricsTrigger:
    """Structural assertion that dashboard_stats.html loads the cache-metrics partial."""

    def test_dashboard_stats_contains_hx_get_for_cache_metrics(self):
        content = _DASHBOARD_STATS_TEMPLATE.read_text()
        assert 'hx-get="/admin/partials/dashboard-cache-metrics"' in content, (
            "dashboard_stats.html must contain an hx-get for /admin/partials/dashboard-cache-metrics"
        )

    def test_dashboard_stats_cache_metrics_trigger_is_load_and_polling(self):
        content = _DASHBOARD_STATS_TEMPLATE.read_text()
        # Find the cache-metrics section block
        idx = content.find('hx-get="/admin/partials/dashboard-cache-metrics"')
        assert idx >= 0, "hx-get for dashboard-cache-metrics not found"
        # Grab a window around it to check hx-trigger
        window = content[max(0, idx - 200) : idx + 200]
        assert "hx-trigger" in window, (
            "No hx-trigger found near the dashboard-cache-metrics hx-get"
        )
        assert "load" in window, (
            "hx-trigger must include 'load' for initial page-load fetch"
        )

    def test_dashboard_stats_cache_metrics_has_container_id(self):
        content = _DASHBOARD_STATS_TEMPLATE.read_text()
        assert "cache-metrics-section" in content, (
            "Expected a container div with id='cache-metrics-section' in dashboard_stats.html"
        )
