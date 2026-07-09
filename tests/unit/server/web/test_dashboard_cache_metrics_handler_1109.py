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
request cycle. set_query_embedding_cache installs a fake cache into the
process-global accessor (Cache Entries card only); clear_* restores it in
teardown. Every other cache card is now sourced from WindowedCacheMetrics
(Story #1294) — those tests install a fake `search_embed_event_writer` on
app.state via `_install_see_writer`/`_windowed_result` instead.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

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


# ---------------------------------------------------------------------------
# Story #1294: fake search_embed_event writer/backend.
#
# Shadow Hit Rate, Shadow Cosine (P50/P05/min/histogram), Over-Cap Queries,
# Provider Embed Calls, Texts Coalesced, Batches Dispatched, Dedup Savings,
# and Audit stats are now sourced from
# app.state.search_embed_event_writer.backend.get_windowed_metrics(), NOT
# from QueryEmbeddingCacheMetrics.snapshot()/CoalescerRegistry.metrics().
# Tests below that exercise those cards install this fake instead of (or in
# addition to) the pre-#1294 fakes above.
# ---------------------------------------------------------------------------


def _windowed_result(
    *,
    shadow_hits: int = 0,
    shadow_misses: int = 0,
    shadow_cosine_p50: Optional[float] = None,
    shadow_cosine_p05: Optional[float] = None,
    shadow_cosine_min: Optional[float] = None,
    shadow_cosine_histogram: Optional[list] = None,
    provider_embed_calls: int = 0,
    texts_coalesced: int = 0,
    batches: int = 0,
    dedup: int = 0,
    long_key: int = 0,
    audit_count: int = 0,
    audit_sum: float = 0.0,
    audit_avg: float = 0.0,
):
    from code_indexer.server.services.windowed_cache_metrics import (
        CacheMetricsAggregate,
        WindowedCacheMetricsResult,
        build_cosine_histogram,
    )

    histogram = (
        shadow_cosine_histogram
        if shadow_cosine_histogram is not None
        else build_cosine_histogram([])
    )
    overall = CacheMetricsAggregate(
        provider_embed_calls=provider_embed_calls,
        texts_coalesced=texts_coalesced,
        batches=batches,
        dedup=dedup,
        long_key=long_key,
        audit_count=audit_count,
        audit_sum=audit_sum,
        audit_avg=audit_avg,
        shadow_cosine_histogram=build_cosine_histogram([]),
    )
    shadow_agg = CacheMetricsAggregate(
        hits=shadow_hits,
        misses=shadow_misses,
        hit_rate=(
            shadow_hits / (shadow_hits + shadow_misses)
            if (shadow_hits + shadow_misses)
            else 0.0
        ),
        shadow_cosine_p50=shadow_cosine_p50,
        shadow_cosine_p05=shadow_cosine_p05,
        shadow_cosine_min=shadow_cosine_min,
        shadow_cosine_histogram=histogram,
    )
    return WindowedCacheMetricsResult(
        overall=overall, by_group={}, by_cache_mode={"shadow": shadow_agg}
    )


class _FakeSeeBackend:
    def __init__(self, result):
        self._result = result

    def get_windowed_metrics(self, from_ts, to_ts):
        return self._result


class _FakeSeeWriter:
    def __init__(self, backend):
        self.backend = backend


_SEE_SENTINEL = object()


def _install_see_writer(app, result):
    original = getattr(app.state, "search_embed_event_writer", _SEE_SENTINEL)
    app.state.search_embed_event_writer = _FakeSeeWriter(_FakeSeeBackend(result))
    return original


def _restore_see_writer(app, original):
    if original is _SEE_SENTINEL:
        try:
            del app.state.search_embed_event_writer
        except AttributeError:
            pass
    else:
        app.state.search_embed_event_writer = original


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

    def test_handler_renders_per_mode_hits(self, client, admin_session_cookie, app):
        """Shadow hit rate must appear, sourced from the windowed result.

        Story #1294: Shadow Hit Rate is now sourced from WindowedCacheMetrics
        (by_cache_mode["shadow"]), not QueryEmbeddingCacheMetrics.snapshot().

        Note (Issue #1257): On-Mode Hit Rate is deliberately NOT asserted
        here — it is sourced from search_event_log's get_hit_rate_counts, not
        from this windowed source (see test_dashboard_on_mode_hit_rate_1257.py
        for the request-denominated on-mode coverage).
        """
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )

        set_query_embedding_cache(_FakeCache(count=42))
        original = _install_see_writer(
            app, _windowed_result(shadow_hits=7, shadow_misses=3)
        )
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            # 42 total entries
            assert "42" in html, f"Expected '42' in HTML, got:\n{html[:500]}"
            # shadow hits = 7, shadow requests = 7+3 = 10 -> hit rate 70.0%
            assert "70.0%" in html, (
                f"Expected shadow hit rate '70.0%' in HTML, got:\n{html[:500]}"
            )
        finally:
            clear_query_embedding_cache()
            _restore_see_writer(app, original)

    def test_handler_renders_cosine_p50(self, client, admin_session_cookie, app):
        """Shadow cosine p50 must appear in the rendered HTML.

        Story #1294: Shadow Cosine P50 is now sourced from
        WindowedCacheMetrics (search_embed_event), not
        QueryEmbeddingCacheMetrics.snapshot().
        """
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )

        set_query_embedding_cache(_FakeCache(count=42))
        original = _install_see_writer(app, _windowed_result(shadow_cosine_p50=0.97))
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            # cosine p50 = 0.97 must appear somewhere in the HTML
            assert "0.9700" in resp.text, (
                f"Expected cosine p50 '0.9700' in HTML, got:\n{resp.text[:500]}"
            )
        finally:
            clear_query_embedding_cache()
            _restore_see_writer(app, original)


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


# ---------------------------------------------------------------------------
# Story #1110 (S6 Chunk B): audit rows in dashboard partial
# ---------------------------------------------------------------------------


class TestAuditRowsRender:
    """Audit rows appear in the cache-metrics partial when audit_total > 0."""

    def test_audit_rows_render_when_audit_total_nonzero(
        self, client, admin_session_cookie, app
    ):
        """When the windowed result includes audit_count=20, the partial must
        render audit stats.

        Story #1294: Audit Samples / Audit Avg Top-10 Overlap are now sourced
        from WindowedCacheMetrics (search_embed_event.audit_sampled/
        audit_cosine), not QueryEmbeddingCacheMetrics.snapshot(). The Audit
        Top-1 Match card is REMOVED — no windowed data source exists for it
        (search_embed_event has no top1-match column).
        """
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )

        set_query_embedding_cache(_FakeCache(count=42))
        original = _install_see_writer(
            app,
            _windowed_result(audit_count=20, audit_sum=17.0, audit_avg=0.85),
        )
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            # Audit sample count must appear
            assert "20" in html, (
                f"Expected audit_total '20' in HTML, got:\n{html[:600]}"
            )
            # Audit Top-1 Match card is removed (Story #1294 — no data source).
            assert "Audit Top-1 Match" not in html
            # Avg overlap = 0.85 -> 85.0% (percentage).
            assert "85.0%" in html, (
                f"Expected overlap avg '85.0%' in HTML, got:\n{html[:600]}"
            )
            assert "0.85" not in html, (
                "Top-10 overlap must render as a percentage, not the bare decimal '0.85'"
            )
        finally:
            clear_query_embedding_cache()
            _restore_see_writer(app, original)


# ---------------------------------------------------------------------------
# Helpers for node-id tests
# ---------------------------------------------------------------------------

# Sentinel distinguishes "attribute not present" from None in app.state manipulation.
_SENTINEL = object()


def _extract_article_cards(html: str) -> list:
    """Split rendered HTML into per-article-card text chunks.

    Returns a list where index 0 is pre-article preamble, index 1 is the
    first card, index 2 is the second card, etc.
    """
    return html.split("<article")


# ---------------------------------------------------------------------------
# Cluster-UX: node name on volatile cards
# ---------------------------------------------------------------------------


class TestNodeIdOnVolatileCards:
    """Story #1294: no cache card carries a per-node label anymore.

    Before Story #1294, Shadow Hit Rate and Shadow Cosine P50 were in-process,
    per-node tallies and carried a "Node <id>" label. Story #1294 re-sourced
    them from WindowedCacheMetrics — a durable, windowed, cluster-aggregated
    read over search_embed_event — so NONE of the cache-metrics cards are
    per-node anymore (Cache Entries and On-Mode Hit Rate were already
    cluster-wide). This class now asserts that absence.
    """

    def test_node_id_absent_from_shadow_and_cosine_cards(
        self, client, admin_session_cookie, app
    ):
        """A known node id injected via app.state.node_id must NOT appear on
        the Shadow Hit Rate or Shadow Cosine P50 cards (both are now
        windowed/cluster-aggregated, not per-node).
        """
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )

        test_node_name = "cidx-worker-77"
        original_node = getattr(app.state, "node_id", _SENTINEL)
        app.state.node_id = test_node_name

        set_query_embedding_cache(_FakeCache(count=5))
        original_see = _install_see_writer(
            app,
            _windowed_result(shadow_hits=4, shadow_misses=1, shadow_cosine_p50=0.99),
        )
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            node_label = f"Node {test_node_name}"
            assert node_label not in html, (
                f"'{node_label}' must NOT appear anywhere in the cache-metrics "
                f"HTML (Story #1294 made all cards cluster-aggregated):\n{html[:600]}"
            )
        finally:
            clear_query_embedding_cache()
            _restore_see_writer(app, original_see)
            if original_node is _SENTINEL:
                try:
                    del app.state.node_id
                except AttributeError:
                    pass
            else:
                app.state.node_id = original_node

    def test_node_id_absent_on_cache_entries_card(
        self, client, admin_session_cookie, app
    ):
        """Cache Entries card (first article) must NOT contain the 'Node ' label."""
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )

        test_node_name = "cidx-worker-99"
        original = getattr(app.state, "node_id", _SENTINEL)
        app.state.node_id = test_node_name

        set_query_embedding_cache(_FakeCache(count=7))
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text

            cards = _extract_article_cards(html)
            assert len(cards) >= 2, "Expected at least one <article> in the HTML"
            first_card = cards[1]  # Cache Entries

            assert f"Node {test_node_name}" not in first_card, (
                f"'Node {test_node_name}' must NOT appear in Cache Entries card:\n{first_card[:400]}"
            )
        finally:
            clear_query_embedding_cache()
            if original is _SENTINEL:
                try:
                    del app.state.node_id
                except AttributeError:
                    pass
            else:
                app.state.node_id = original


# ---------------------------------------------------------------------------
# Story #1149 (BLOCKING): long_key counter surfaced through the front door
# ---------------------------------------------------------------------------


class TestLongKeyFrontDoor:
    """Story #1149 (BLOCKING): long_key counter must be observable through the front door.

    The rejection item requires:
    - dashboard_cache_metrics_partial extracts long_key from snapshot() into the
      cache_metrics SimpleNamespace
    - the rendered dashboard_cache_metrics.html partial contains the long_key value
    """

    def test_handler_puts_long_key_in_namespace(
        self, client, admin_session_cookie, app
    ):
        """When the windowed result includes long_key=17, the rendered HTML
        contains '17'.

        Story #1294: long_key (Over-Cap Queries) is now sourced from
        WindowedCacheMetrics.overall.long_key, not
        QueryEmbeddingCacheMetrics.snapshot()["long_key"].
        """
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )

        set_query_embedding_cache(_FakeCache(count=5))
        original = _install_see_writer(app, _windowed_result(long_key=17))
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            assert "17" in html, (
                f"Expected long_key value '17' in rendered dashboard HTML, got:\n{html[:600]}"
            )
        finally:
            clear_query_embedding_cache()
            _restore_see_writer(app, original)

    def test_handler_renders_long_key_zero_when_no_queries_skipped(
        self, client, admin_session_cookie, app
    ):
        """When long_key=0 (no over-cap queries), the handler must still render 0
        (or '--') without raising — the field is always present in the namespace.
        """
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )

        set_query_embedding_cache(_FakeCache(count=0))
        original = _install_see_writer(app, _windowed_result(long_key=0))
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            # Must not blow up — long_key=0 is a valid state
        finally:
            clear_query_embedding_cache()
            _restore_see_writer(app, original)


# ---------------------------------------------------------------------------
# Story #1146 (BLOCKING anti-orphan): coalescer dedup counters visible on dashboard
# ---------------------------------------------------------------------------


class TestCoalescerCountersRender:
    """Story #1146 anti-orphan / Story #1294: dedup counters must be rendered
    in the dashboard cache-metrics partial.

    Originally sourced from the per-node in-memory CoalescerRegistry (Story
    #1146); Story #1294 re-sourced them from WindowedCacheMetrics.overall
    (durable, windowed, cluster-aggregated over search_embed_event). These
    tests now install a fake search_embed_event_writer instead of a fake
    CoalescerRegistry.
    """

    def test_coalescer_provider_embed_calls_rendered(
        self, client, admin_session_cookie, app
    ):
        """provider_embed_calls must appear in the rendered dashboard HTML."""
        original = _install_see_writer(app, _windowed_result(provider_embed_calls=42))
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            assert "42" in html, (
                f"Expected provider_embed_calls '42' in rendered HTML, got:\n{html[:800]}"
            )
        finally:
            _restore_see_writer(app, original)

    def test_coalescer_texts_coalesced_rendered(
        self, client, admin_session_cookie, app
    ):
        """texts_coalesced must appear in the rendered dashboard HTML."""
        original = _install_see_writer(app, _windowed_result(texts_coalesced=199))
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            assert "199" in html, (
                f"Expected texts_coalesced '199' in rendered HTML, got:\n{html[:800]}"
            )
        finally:
            _restore_see_writer(app, original)

    def test_coalescer_dedup_savings_rendered(self, client, admin_session_cookie, app):
        """dedup_savings must appear in the rendered dashboard HTML."""
        original = _install_see_writer(app, _windowed_result(dedup=77))
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            assert "77" in html, (
                f"Expected dedup_savings '77' in rendered HTML, got:\n{html[:800]}"
            )
        finally:
            _restore_see_writer(app, original)

    def test_coalescer_batches_dispatched_rendered(
        self, client, admin_session_cookie, app
    ):
        """batches_dispatched must appear in the rendered dashboard HTML."""
        original = _install_see_writer(app, _windowed_result(batches=13))
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            assert "13" in html, (
                f"Expected batches_dispatched '13' in rendered HTML, got:\n{html[:800]}"
            )
        finally:
            _restore_see_writer(app, original)


# ---------------------------------------------------------------------------
# Story #1152 (BLOCKING): populated histogram must not crash with | log(10)
# ---------------------------------------------------------------------------


def _histogram_with_high_and_low_bucket() -> list:
    """Sparse 40-bucket histogram: bucket 38 [0.90,0.95) has 500 hits, bucket
    20 has 1 hit (exercises log10 scaling). All others are 0 (Story #1152).
    """
    histogram = []
    for i in range(40):
        lo = round(-1.0 + i * 0.05, 10)
        hi = round(-1.0 + (i + 1) * 0.05, 10)
        if i == 38:
            histogram.append((lo, hi, 500))
        elif i == 20:
            histogram.append((lo, hi, 1))
        else:
            histogram.append((lo, hi, 0))
    return histogram


class TestPopulatedHistogramRender:
    """Story #1152: populated histogram must render without the | log(10) crash.
    Story #1294: histogram now comes from WindowedCacheMetrics
    (by_cache_mode["shadow"].shadow_cosine_histogram), not
    QueryEmbeddingCacheMetrics.snapshot().

    The old template contained:
      {%- set log_len = ((count + 1) | log(10) / ...) -%}
    Jinja2 has no built-in `log` filter -> TemplateAssertionError -> HTTP 500.

    These tests MUST fail against the old | log(10) template and PASS after the
    Python-precompute fix is applied (bar percentages computed in routes.py).
    """

    def test_populated_histogram_renders_without_crash(
        self, client, admin_session_cookie, app
    ):
        """HTTP 200 (not 500) when the windowed result includes a populated
        histogram.

        This test FAILS if the template still uses `| log(10)` (Jinja raises
        TemplateAssertionError which FastAPI converts to HTTP 500).
        """
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )

        set_query_embedding_cache(_FakeCache(count=10))
        original = _install_see_writer(
            app,
            _windowed_result(
                shadow_cosine_histogram=_histogram_with_high_and_low_bucket(),
                shadow_cosine_min=0.03,
                shadow_cosine_p05=0.92,
                shadow_cosine_p50=0.94,
            ),
        )
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200, (
                f"Expected HTTP 200 for populated histogram, got {resp.status_code}. "
                f"If 500, the | log(10) Jinja filter crash is still present.\n"
                f"Response body (first 800 chars):\n{resp.text[:800]}"
            )
        finally:
            clear_query_embedding_cache()
            _restore_see_writer(app, original)

    def test_populated_histogram_shows_bar_elements_and_stats(
        self, client, admin_session_cookie, app
    ):
        """Rendered HTML must contain bar elements + raw counts + P50/min/P05.

        Asserts the Python-precompute path produces visible chart content and
        the summary statistics are surfaced from the windowed result.
        """
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )

        set_query_embedding_cache(_FakeCache(count=10))
        original = _install_see_writer(
            app,
            _windowed_result(
                shadow_cosine_histogram=_histogram_with_high_and_low_bucket(),
                shadow_cosine_min=0.03,
                shadow_cosine_p05=0.92,
                shadow_cosine_p50=0.94,
            ),
        )
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            # Raw counts: the big bucket has 500 hits
            assert "500" in html, (
                f"Expected bucket count '500' in rendered HTML, got:\n{html[:800]}"
            )
            # CSS bar: at least one bar fill with a width% style must appear
            assert "width:" in html, (
                f"Expected CSS 'width:' bar fill in rendered histogram, got:\n{html[:800]}"
            )
            # P50 summary: 0.9400
            assert "0.9400" in html, (
                f"Expected P50 '0.9400' in rendered HTML, got:\n{html[:800]}"
            )
            # Min summary: 0.0300
            assert "0.0300" in html, (
                f"Expected min '0.0300' in rendered HTML, got:\n{html[:800]}"
            )
            # P05 summary: 0.9200
            assert "0.9200" in html, (
                f"Expected P05 '0.9200' in rendered HTML, got:\n{html[:800]}"
            )
        finally:
            clear_query_embedding_cache()
            _restore_see_writer(app, original)


# ---------------------------------------------------------------------------
# CSS bar chart: contained layout, empty state, no ASCII overflow
# ---------------------------------------------------------------------------


def _fetch_cache_metrics_html(client, app, windowed_result, cache_count: int) -> str:
    """Shared helper: install fake cache+windowed writer, fetch the partial,
    return HTML.

    The shared abstraction that keeps TestCSSBarChartRender/
    TestCosineChartReverseOrder test methods DRY — each method calls this
    directly, so there is no duplicated setup/teardown. Raises
    AssertionError if the response is not HTTP 200.
    """
    from code_indexer.server.services.governed_call import (
        set_query_embedding_cache,
        clear_query_embedding_cache,
    )

    set_query_embedding_cache(_FakeCache(count=cache_count))
    original = _install_see_writer(app, windowed_result)
    try:
        resp = client.get("/admin/partials/dashboard-cache-metrics")
        assert resp.status_code == 200, (
            f"Expected HTTP 200, got {resp.status_code}: {resp.text[:400]}"
        )
        return str(resp.text)
    finally:
        clear_query_embedding_cache()
        _restore_see_writer(app, original)


class TestCSSBarChartRender:
    """CSS bar chart template: contained layout, empty state, no ASCII overflow.

    These tests validate the replacement of the broken ASCII `white-space: pre`
    chart (which overflowed horizontally) with a proper CSS bar chart.
    Each test calls _fetch_cache_metrics_html — the shared DRY abstraction.
    """

    def test_css_bars_present_when_populated(self, client, admin_session_cookie, app):
        """When histogram has non-zero buckets, rendered HTML contains CSS bar
        fills with `width:` style attribute, not `#` ASCII characters.
        """
        html = _fetch_cache_metrics_html(
            client,
            app,
            _windowed_result(
                shadow_cosine_histogram=_histogram_with_high_and_low_bucket(),
                shadow_cosine_min=0.03,
                shadow_cosine_p05=0.92,
                shadow_cosine_p50=0.94,
            ),
            10,
        )
        assert "width:" in html, (
            f"Expected CSS 'width:' on bar fill elements, got:\n{html[:800]}"
        )
        assert "500" in html, (
            f"Expected bucket count '500' in rendered HTML, got:\n{html[:800]}"
        )

    def test_empty_state_when_all_zero(self, client, admin_session_cookie, app):
        """When all histogram bucket counts are 0, render the empty-state message
        instead of 40 empty bar rows.
        """
        html = _fetch_cache_metrics_html(client, app, _windowed_result(), 0)
        assert "No shadow cosine samples" in html, (
            f"Expected empty-state message 'No shadow cosine samples' in HTML "
            f"when all buckets are zero, got:\n{html[:800]}"
        )

    def test_no_whitespace_pre_in_chart_body(self, client, admin_session_cookie, app):
        """The chart body must NOT use `white-space: pre` which caused horizontal
        overflow when Jinja whitespace-control stripped newlines between buckets.
        """
        html = _fetch_cache_metrics_html(
            client,
            app,
            _windowed_result(
                shadow_cosine_histogram=_histogram_with_high_and_low_bucket(),
                shadow_cosine_min=0.03,
                shadow_cosine_p05=0.92,
                shadow_cosine_p50=0.94,
            ),
            10,
        )
        assert "white-space: pre" not in html, (
            "Found 'white-space: pre' in rendered HTML — this causes horizontal "
            "overflow. The chart must use a block/flex layout instead."
        )


# ---------------------------------------------------------------------------
# Visibility fix: high-cosine buckets must render at the top (reversed order)
# ---------------------------------------------------------------------------


def _histogram_only_high_bucket() -> list:
    """Histogram where ONLY the last bucket [0.95, 1.00) has a non-zero count.

    Mirrors the real-world scenario: shadow-cosine audits on cached embeddings
    of the SAME query always cluster at ~1.0, so index 39 (the highest bucket)
    is the only populated one. All 39 lower buckets are zero.
    """
    histogram = []
    for i in range(40):
        lo = round(-1.0 + i * 0.05, 10)
        hi = round(-1.0 + (i + 1) * 0.05, 10)
        count = 30 if i == 39 else 0  # only [0.95, 1.00) has samples
        histogram.append((lo, hi, count))
    return histogram


class TestCosineChartReverseOrder:
    """Visibility fix: populated high-cosine buckets must appear FIRST (top of chart).

    Before the fix the chart rendered buckets in ascending order (index 0 first,
    index 39 last).  With max-height: 28rem only ~22 of 40 rows were visible, so
    the single populated bucket at index 39 was scrolled off the bottom.

    After the fix ({% for b in histogram_bars | reverse %}) index 39 renders
    FIRST, making the data immediately visible without scrolling.
    """

    def test_high_cosine_bucket_renders_before_low_cosine_bucket(
        self, client, admin_session_cookie, app
    ):
        """[0.95, 1.00) row must appear earlier in the HTML than [-1.00, -0.95).

        With only the [0.95, 1.00) bucket populated the reversed chart places it
        at the top; the always-empty [-1.00, -0.95) row scrolls below.
        """
        html = _fetch_cache_metrics_html(
            client,
            app,
            _windowed_result(
                shadow_cosine_histogram=_histogram_only_high_bucket(),
                shadow_cosine_min=0.97,
                shadow_cosine_p05=0.98,
                shadow_cosine_p50=0.99,
            ),
            5,
        )

        # Both bucket labels must be present (all 40 rows still render).
        assert "[0.95, 1.00)" in html, "Expected '[0.95, 1.00)' label in rendered HTML"
        assert "[-1.00, -0.95)" in html, (
            "Expected '[-1.00, -0.95)' label in rendered HTML"
        )

        # After reversal the [0.95, 1.00) row must appear BEFORE [-1.00, -0.95).
        idx_high = html.index("[0.95, 1.00)")
        idx_low = html.index("[-1.00, -0.95)")
        assert idx_high < idx_low, (
            f"Expected [0.95, 1.00) to appear before [-1.00, -0.95) in the HTML "
            f"(populated data at top), but got offsets: high={idx_high}, low={idx_low}. "
            "The histogram_bars loop must use '| reverse' so data-rich buckets "
            "are visible without scrolling."
        )

    def test_populated_bucket_count_visible_in_first_rows(
        self, client, admin_session_cookie, app
    ):
        """The count '30' from the [0.95, 1.00) bucket must appear in the HTML
        near the top (before any empty bucket rows), confirming data is not
        scrolled off.
        """
        html = _fetch_cache_metrics_html(
            client,
            app,
            _windowed_result(
                shadow_cosine_histogram=_histogram_only_high_bucket(),
                shadow_cosine_min=0.97,
                shadow_cosine_p05=0.98,
                shadow_cosine_p50=0.99,
            ),
            5,
        )

        # The populated [0.95, 1.00) row should appear before the first empty
        # zero-count row in the HTML (empty rows have count 0).
        idx_count_30 = html.find(">30<")
        # The label for the low bucket (always empty) marks where empty rows start.
        idx_low_label = html.find("[-1.00, -0.95)")
        assert idx_count_30 != -1, "Expected count '30' in rendered HTML"
        assert idx_count_30 < idx_low_label, (
            f"Count '30' (at offset {idx_count_30}) must appear before "
            f"the [-1.00, -0.95) row (at offset {idx_low_label}), confirming "
            "that populated data renders at the top of the chart."
        )

    def test_max_height_is_compact(self, client, admin_session_cookie, app):
        """The cosine-chart container must use max-height: 14rem (not 28rem).

        A compact container ensures the visible viewport starts at the top
        (where data is after reversal) without wasting vertical space on the
        always-empty tail.
        """
        html = _fetch_cache_metrics_html(
            client,
            app,
            _windowed_result(
                shadow_cosine_histogram=_histogram_only_high_bucket(),
                shadow_cosine_min=0.97,
                shadow_cosine_p05=0.98,
                shadow_cosine_p50=0.99,
            ),
            5,
        )
        assert "max-height: 14rem" in html, (
            "Expected 'max-height: 14rem' on the cosine-chart container. "
            "The container was previously 28rem which hid populated buckets "
            "when they were at the bottom."
        )
        assert "max-height: 28rem" not in html, (
            "Found old 'max-height: 28rem' still in the HTML — replace with 14rem."
        )
