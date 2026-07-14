"""Integration tests for the windowed, cluster-aggregated cache-metrics
dashboard (Story #1294, Epic #1288).

Verifies dashboard_cache_metrics_partial (routes.py) re-sources every cache
card (except Cache Entries) from WindowedCacheMetrics fed by
app.state.search_embed_event_writer.backend.get_windowed_metrics(), plumbs a
`cache_window` query param through to the from_ts/to_ts window, swaps the
"volatile / per-node / resets on restart" badge text for
"durable / windowed / cluster-aggregated", drops the Audit Top-1 Match card
(no windowed data source exists for it), fails open when the writer/backend
is absent or raises, and leaves Cache Entries + On-Mode Hit Rate (Issue
#1257) untouched.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.services.windowed_cache_metrics import (
    CacheMetricsAggregate,
    WindowedCacheMetricsResult,
)

_DASHBOARD_STATS_TEMPLATE = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src/code_indexer/server/web/templates/partials/dashboard_stats.html"
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSeeBackend:
    """Fake search_embed_event backend with a controllable get_windowed_metrics()."""

    def __init__(
        self,
        result: Optional[WindowedCacheMetricsResult] = None,
        raise_error: bool = False,
    ):
        self._result = result
        self._raise_error = raise_error
        self.last_call: Optional[tuple] = None

    def get_windowed_metrics(self, from_ts: float, to_ts: float):
        self.last_call = (from_ts, to_ts)
        if self._raise_error:
            raise RuntimeError("simulated backend failure")
        return self._result


class _FakeSeeWriter:
    def __init__(self, backend: _FakeSeeBackend):
        self.backend = backend


def _build_result(
    *,
    shadow_hits=0,
    shadow_misses=0,
    shadow_p50=None,
    shadow_p05=None,
    shadow_min=None,
    provider_embed_calls=0,
    texts_coalesced=0,
    batches=0,
    dedup=0,
    long_key=0,
    audit_count=0,
    audit_sum=0.0,
    audit_avg=0.0,
) -> WindowedCacheMetricsResult:
    histogram = [
        (round(-1.0 + i * 0.05, 10), round(-1.0 + (i + 1) * 0.05, 10), 0)
        for i in range(40)
    ]
    overall = CacheMetricsAggregate(
        hits=shadow_hits,
        misses=shadow_misses,
        provider_embed_calls=provider_embed_calls,
        texts_coalesced=texts_coalesced,
        batches=batches,
        dedup=dedup,
        long_key=long_key,
        audit_count=audit_count,
        audit_sum=audit_sum,
        audit_avg=audit_avg,
        shadow_cosine_histogram=histogram,
    )
    shadow_agg = CacheMetricsAggregate(
        hits=shadow_hits,
        misses=shadow_misses,
        hit_rate=(shadow_hits / (shadow_hits + shadow_misses))
        if (shadow_hits + shadow_misses)
        else 0.0,
        shadow_cosine_p50=shadow_p50,
        shadow_cosine_p05=shadow_p05,
        shadow_cosine_min=shadow_min,
        shadow_cosine_histogram=histogram,
    )
    return WindowedCacheMetricsResult(
        overall=overall,
        by_group={},
        by_cache_mode={"shadow": shadow_agg},
    )


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_dashboard_cache_metrics_handler_1109.py pattern)
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
    login_page = client.get("/login")
    assert login_page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match, "Could not extract CSRF token from login page"
    csrf_token = match.group(1)

    login_resp = client.post(
        "/login",
        data={"username": "admin", "password": "admin", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert login_resp.status_code == 303, f"Form login failed: {login_resp.status_code}"
    assert "session" in login_resp.cookies
    for name, value in login_resp.cookies.items():
        client.cookies.set(name, value)
    return login_resp.cookies


_SENTINEL = object()


def _install_writer(app, backend: _FakeSeeBackend):
    original = getattr(app.state, "search_embed_event_writer", _SENTINEL)
    app.state.search_embed_event_writer = _FakeSeeWriter(backend)
    return original


def _restore_writer(app, original):
    if original is _SENTINEL:
        try:
            del app.state.search_embed_event_writer
        except AttributeError:
            pass
    else:
        app.state.search_embed_event_writer = original


# ---------------------------------------------------------------------------
# Window plumbing
# ---------------------------------------------------------------------------


class TestCacheWindowPlumbing:
    def test_default_window_used_when_no_param(self, client, admin_session_cookie, app):
        backend = _FakeSeeBackend(_build_result())
        original = _install_writer(app, backend)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            assert backend.last_call is not None
            from_ts, to_ts = backend.last_call
            # Default window must be a sane positive span (24h == 86400s).
            assert 86000 <= (to_ts - from_ts) <= 86500
        finally:
            _restore_writer(app, original)

    def test_cache_window_query_param_plumbed_to_backend(
        self, client, admin_session_cookie, app
    ):
        backend = _FakeSeeBackend(_build_result())
        original = _install_writer(app, backend)
        try:
            resp = client.get(
                "/admin/partials/dashboard-cache-metrics?cache_window=900"
            )
            assert resp.status_code == 200
            assert backend.last_call is not None
            from_ts, to_ts = backend.last_call
            assert 850 <= (to_ts - from_ts) <= 950
        finally:
            _restore_writer(app, original)


# ---------------------------------------------------------------------------
# Card resourcing
# ---------------------------------------------------------------------------


class TestCardsResourcedFromWindowedResult:
    def test_shadow_cosine_p50_from_by_cache_mode_shadow(
        self, client, admin_session_cookie, app
    ):
        """Shadow Cosine P50 stays sourced from search_embed_event's
        by_cache_mode["shadow"] aggregate (per-sample distributions are
        correctly operation-based). Bug #1391: the Shadow Hit Rate card
        itself moved OFF this source onto search_event_log -- see
        test_dashboard_hit_rate_cards_windowing_1391.py for that coverage --
        so shadow_agg.hits/misses here are irrelevant to the hit-rate card
        and deliberately NOT asserted.
        """
        backend = _FakeSeeBackend(
            _build_result(shadow_hits=7, shadow_misses=3, shadow_p50=0.97)
        )
        original = _install_writer(app, backend)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            assert "0.9700" in html, (
                f"Expected shadow cosine p50 0.9700 in HTML:\n{html[:800]}"
            )
        finally:
            _restore_writer(app, original)

    def test_provider_embed_calls_texts_coalesced_batches_dedup_long_key(
        self, client, admin_session_cookie, app
    ):
        backend = _FakeSeeBackend(
            _build_result(
                provider_embed_calls=42,
                texts_coalesced=199,
                batches=13,
                dedup=77,
                long_key=9,
            )
        )
        original = _install_writer(app, backend)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            assert "42" in html
            assert "199" in html
            assert "13" in html
            assert "77" in html
            assert "9" in html
        finally:
            _restore_writer(app, original)

    def test_audit_count_sum_avg_rendered(self, client, admin_session_cookie, app):
        backend = _FakeSeeBackend(
            _build_result(audit_count=20, audit_sum=17.0, audit_avg=0.85)
        )
        original = _install_writer(app, backend)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            assert "20" in html
            assert "85.0%" in html
        finally:
            _restore_writer(app, original)

    def test_audit_top1_match_card_removed(self, client, admin_session_cookie, app):
        """No windowed data source exists for top1-match; the card must be gone."""
        backend = _FakeSeeBackend(
            _build_result(audit_count=5, audit_sum=4.0, audit_avg=0.8)
        )
        original = _install_writer(app, backend)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            assert "Audit Top-1 Match" not in resp.text
        finally:
            _restore_writer(app, original)


# ---------------------------------------------------------------------------
# Badge swap
# ---------------------------------------------------------------------------


class TestBadgeSwap:
    def test_durable_windowed_cluster_aggregated_badge_absent(
        self, client, admin_session_cookie, app
    ):
        """Bug #1311: the "durable · windowed · cluster-aggregated" badge and
        footer note-prefix (introduced in Story #1294 to distinguish
        migrated-vs-not cards) are now pure noise -- every card is DB-sourced,
        so the badge distinguishes nothing and its chip visually overflows
        the card. It must be removed entirely, not consolidated.
        """
        backend = _FakeSeeBackend(_build_result())
        original = _install_writer(app, backend)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            assert "cache-metrics-durable-badge" not in html
            assert "cache-metrics-durable-note" not in html
            assert "cluster-aggregated" not in html.lower()
        finally:
            _restore_writer(app, original)

    def test_volatile_resets_on_restart_text_absent(
        self, client, admin_session_cookie, app
    ):
        backend = _FakeSeeBackend(_build_result())
        original = _install_writer(app, backend)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            assert "resets on restart" not in html.lower()
            assert ">volatile<" not in html.lower()
        finally:
            _restore_writer(app, original)


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_renders_when_writer_absent(self, client, admin_session_cookie, app):
        original = getattr(app.state, "search_embed_event_writer", _SENTINEL)
        try:
            app.state.search_embed_event_writer = None
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
        finally:
            _restore_writer(app, original)

    def test_renders_when_backend_raises(self, client, admin_session_cookie, app):
        backend = _FakeSeeBackend(result=None, raise_error=True)
        original = _install_writer(app, backend)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200, (
                f"Expected fail-open 200, got {resp.status_code}: {resp.text[:400]}"
            )
        finally:
            _restore_writer(app, original)


# ---------------------------------------------------------------------------
# Non-regression: Cache Entries + On-Mode Hit Rate
# ---------------------------------------------------------------------------


class TestNonRegression:
    def test_cache_entries_still_sourced_from_qec_total_entries(
        self, client, admin_session_cookie, app
    ):
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )

        class _FakeCache:
            def total_entries(self):
                return 123

        backend = _FakeSeeBackend(_build_result())
        original = _install_writer(app, backend)
        set_query_embedding_cache(_FakeCache())
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            assert "123" in resp.text
        finally:
            clear_query_embedding_cache()
            _restore_writer(app, original)

    def test_on_mode_hit_rate_unaffected_by_windowed_source(
        self, client, admin_session_cookie, app
    ):
        """On-Mode Hit Rate (Issue #1257) stays sourced from search_event_log's
        get_hit_rate_counts, NOT from the new windowed search_embed_event source.

        Bug #1391: get_hit_rate_counts now also receives from_ts/to_ts (both
        modes), so the fake must accept them; only the "on" result is
        asserted here (Shadow Hit Rate windowing/denomination coverage lives
        in test_dashboard_hit_rate_cards_windowing_1391.py).
        """

        class _FakeSelBackend:
            def get_hit_rate_counts(self, mode, from_ts=None, to_ts=None):
                if mode == "on":
                    return {"hits": 5, "requests": 8}
                return {"hits": 0, "requests": 0}

        class _FakeSelWriter:
            backend = _FakeSelBackend()

        backend = _FakeSeeBackend(_build_result())
        original_see = _install_writer(app, backend)
        original_sel = getattr(app.state, "search_event_log_writer", _SENTINEL)
        app.state.search_event_log_writer = _FakeSelWriter()
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            # 5/8 * 100 = 62.5%
            assert "62.5%" in html, (
                f"Expected on-mode hit rate 62.5% in HTML:\n{html[:800]}"
            )
        finally:
            _restore_writer(app, original_see)
            if original_sel is _SENTINEL:
                try:
                    del app.state.search_event_log_writer
                except AttributeError:
                    pass
            else:
                app.state.search_event_log_writer = original_sel


# ---------------------------------------------------------------------------
# Time-window selector UI (dashboard_stats.html)
# ---------------------------------------------------------------------------


class TestWindowSelectorUI:
    def test_dashboard_stats_contains_cache_window_selector(self):
        content = _DASHBOARD_STATS_TEMPLATE.read_text()
        assert 'id="cache-window"' in content, (
            'Expected a <select id="cache-window"> time-window selector in '
            "dashboard_stats.html"
        )

    def test_cache_metrics_section_includes_and_reacts_to_window_selector(self):
        content = _DASHBOARD_STATS_TEMPLATE.read_text()
        idx = content.find('hx-get="/admin/partials/dashboard-cache-metrics"')
        assert idx >= 0
        window = content[max(0, idx - 300) : idx + 300]
        assert "cache-window" in window, (
            "The dashboard-cache-metrics hx-get element must reference "
            "#cache-window (via hx-include and/or hx-trigger)"
        )


# ---------------------------------------------------------------------------
# Rendered-selection regression: which <option> actually gets `selected`
# ---------------------------------------------------------------------------


def _render_dashboard_stats(**overrides) -> str:
    """Render partials/dashboard_stats.html via the production Jinja2Templates
    instance (routes.templates), with a minimal-but-complete context. Any
    key NOT passed in overrides is simply absent from the context (Jinja
    Undefined) — this is what reproduces the initial-load bug where
    cache_window is never set by the caller.
    """
    from code_indexer.server.web import routes as routes_module

    base_context: Dict[str, Any] = {
        "job_counts": None,
        "repo_counts": None,
        "recent_jobs": [],
        "api_metrics": {},
        "time_filter": "24h",
        "recent_filter": "24h",
        "api_filter": 900,
        "active_deactivations": 0,
        "has_provider_results": False,
    }
    base_context.update(overrides)
    template = routes_module.templates.get_template("partials/dashboard_stats.html")
    return str(template.render(**base_context))


def _selected_cache_window_value(html: str) -> Optional[str]:
    """Parse the rendered <select id="cache-window"> block and return the
    `value` of whichever <option> carries the `selected` attribute, or None
    if no option is selected (reproduces the browser-defaults-to-first-option
    behavior when no option has `selected`).
    """
    select_match = re.search(r'<select id="cache-window".*?</select>', html, re.DOTALL)
    assert select_match, 'Expected <select id="cache-window"> block in rendered HTML'
    select_html = select_match.group(0)
    for option_match in re.finditer(r'<option value="([^"]+)"([^>]*)>', select_html):
        value, attrs = option_match.group(1), option_match.group(2)
        if "selected" in attrs:
            return value
    return None


class TestWindowSelectorRenderedSelection:
    """Rendered-HTML regression for the window-selector default-option bug.

    Code review (pre-commit fix): dashboard_stats_partial's render context
    never passed cache_window, so on initial load cache_window was Jinja
    Undefined — `== 86400` is False AND `is none` is False (Undefined != None)
    — so NO option got `selected`, and the browser fell back to displaying
    the FIRST option ("Last 15 Minutes") while the cards actually rendered
    24h data. These tests parse the RENDERED selector (not raw template
    text) to catch that class of defect.
    """

    def test_default_load_selects_24_hours_option_when_cache_window_undefined(self):
        """Initial load: cache_window is absent from context (Undefined) —
        the 86400 ("Last 24 Hours") option must still be marked selected,
        matching the window the cards actually render (Story #1294 default).
        """
        html = _render_dashboard_stats()  # cache_window intentionally omitted
        assert _selected_cache_window_value(html) == "86400", (
            "Expected the 'Last 24 Hours' (86400) option to be selected when "
            "cache_window is undefined (initial load), matching the cards' "
            "actual default window."
        )

    def test_explicit_cache_window_selects_matching_option(self):
        """An explicit cache_window=3600 must select the 'Last 1 Hour' option."""
        html = _render_dashboard_stats(cache_window=3600)
        assert _selected_cache_window_value(html) == "3600", (
            "Expected the 'Last 1 Hour' (3600) option to be selected when "
            "cache_window=3600 is passed explicitly."
        )
