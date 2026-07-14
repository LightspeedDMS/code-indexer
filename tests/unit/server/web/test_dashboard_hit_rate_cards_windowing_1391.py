"""Regression tests for GitHub Issue #1391.

Two related defects on the dashboard's cache-metrics panel hit-rate cards:

Defect 1 -- On-Mode Hit Rate ignores the Time Window selector.
    dashboard_cache_metrics_partial() computes from_ts/to_ts (the selected
    cache_window) and passes them to get_windowed_metrics() for every other
    card, but called get_hit_rate_counts("on") with NO bounds at all, so the
    On-Mode Hit Rate card always reflected the entire search_event_log table
    regardless of the Time Window selector.

Defect 2 -- Shadow Hit Rate used an inconsistent (operation-denominated)
    denominator. It was sourced from windowed.by_cache_mode["shadow"] over
    search_embed_event (one row per NEEDED embed per provider), while
    On-Mode Hit Rate is sourced from search_event_log (one row per user
    request). Fix: Shadow Hit Rate now shares the SAME request-denominated
    source/window as On-Mode: sel_backend.get_hit_rate_counts("shadow",
    from_ts, to_ts).

These tests drive the real /admin/partials/dashboard-cache-metrics route
through the FastAPI TestClient front door (matching the pattern already
used by test_dashboard_on_mode_hit_rate_1257.py /
test_dashboard_windowed_cache_metrics_1294.py) and must FAIL against the
pre-fix routes.py.
"""

from __future__ import annotations

import re
import time
from typing import Optional

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
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


def _extract_article_cards(html: str) -> list:
    return html.split("<article")


# ---------------------------------------------------------------------------
# Fake search_event_log writer/backend that records every call's window args
# ---------------------------------------------------------------------------


class _RecordingSelBackend:
    """Fake SearchEventLogSqliteBackend/PostgresBackend stand-in that records
    every get_hit_rate_counts() call (mode, from_ts, to_ts) and returns a
    canned result per mode."""

    def __init__(self, on_result: dict, shadow_result: dict):
        self._on_result = on_result
        self._shadow_result = shadow_result
        self.calls: list = []

    def get_hit_rate_counts(
        self,
        mode: str,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
    ) -> dict:
        self.calls.append((mode, from_ts, to_ts))
        if mode == "shadow":
            return dict(self._shadow_result)
        return dict(self._on_result)


class _RecordingSelWriter:
    def __init__(self, backend: _RecordingSelBackend):
        self.backend = backend


def _install_sel_writer(app, backend: _RecordingSelBackend):
    original = getattr(app.state, "search_event_log_writer", _SENTINEL)
    app.state.search_event_log_writer = _RecordingSelWriter(backend)
    return original


def _restore_sel_writer(app, original):
    if original is _SENTINEL:
        try:
            del app.state.search_event_log_writer
        except AttributeError:
            pass
    else:
        app.state.search_event_log_writer = original


# ---------------------------------------------------------------------------
# Fake search_embed_event writer/backend (cosine cards regression guard)
# ---------------------------------------------------------------------------


class _FakeSeeBackend:
    def __init__(self, result):
        self._result = result

    def get_windowed_metrics(self, from_ts, to_ts):
        return self._result


class _FakeSeeWriter:
    def __init__(self, backend):
        self.backend = backend


def _install_see_writer(app, result):
    original = getattr(app.state, "search_embed_event_writer", _SENTINEL)
    app.state.search_embed_event_writer = _FakeSeeWriter(_FakeSeeBackend(result))
    return original


def _restore_see_writer(app, original):
    if original is _SENTINEL:
        try:
            del app.state.search_embed_event_writer
        except AttributeError:
            pass
    else:
        app.state.search_embed_event_writer = original


def _windowed_result_with_cosine(shadow_cosine_p50: float):
    from code_indexer.server.services.windowed_cache_metrics import (
        CacheMetricsAggregate,
        WindowedCacheMetricsResult,
        build_cosine_histogram,
    )

    overall = CacheMetricsAggregate(shadow_cosine_histogram=build_cosine_histogram([]))
    shadow_agg = CacheMetricsAggregate(
        hits=999,  # deliberately absurd -- must NOT leak into the hit-rate card
        misses=1,
        shadow_cosine_p50=shadow_cosine_p50,
        shadow_cosine_histogram=build_cosine_histogram([]),
    )
    return WindowedCacheMetricsResult(
        overall=overall, by_group={}, by_cache_mode={"shadow": shadow_agg}
    )


# ---------------------------------------------------------------------------
# Defect 1: On-Mode Hit Rate must respect cache_window
# ---------------------------------------------------------------------------


class TestOnModeHitRateRespectsTimeWindow:
    def test_on_mode_call_receives_from_ts_and_to_ts(
        self, client, admin_session_cookie, app
    ):
        """get_hit_rate_counts("on", ...) must be called WITH the same
        from_ts/to_ts window computed for every other windowed card, not
        with no bounds at all."""
        backend = _RecordingSelBackend(
            on_result={"hits": 1, "requests": 2},
            shadow_result={"hits": 0, "requests": 0},
        )
        original = _install_sel_writer(app, backend)
        try:
            resp = client.get(
                "/admin/partials/dashboard-cache-metrics?cache_window=900"
            )
            assert resp.status_code == 200
            on_calls = [c for c in backend.calls if c[0] == "on"]
            assert len(on_calls) == 1, (
                f"Expected exactly one 'on' call, got {backend.calls}"
            )
            _, from_ts, to_ts = on_calls[0]
            assert from_ts is not None and to_ts is not None, (
                "On-Mode Hit Rate call must receive non-None from_ts/to_ts "
                f"bounds, got from_ts={from_ts}, to_ts={to_ts}"
            )
            assert 850 <= (to_ts - from_ts) <= 950, (
                f"Expected a ~900s window, got from_ts={from_ts}, to_ts={to_ts}"
            )
        finally:
            _restore_sel_writer(app, original)

    def test_on_mode_hit_rate_value_changes_with_cache_window(
        self, client, admin_session_cookie, app, tmp_path
    ):
        """A row 2000s old must be included with a 1-hour window but EXCLUDED
        with a 900s window -- proving the rendered percentage actually
        changes when the Time Window selector changes."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
            SearchEventRecord,
        )

        db_path = str(tmp_path / "sel_1391_window.db")
        backend = SearchEventLogSqliteBackend(db_path)
        now = time.time()
        backend.insert_batch(
            [
                SearchEventRecord(
                    timestamp=now - 2000,
                    username="alice",
                    repo_alias="repo1",
                    search_type="semantic",
                    query_text="q",
                    voyage_cache_hit=True,
                    voyage_cache_mode="on",
                    voyage_latency_ms=1,
                    cohere_cache_hit=None,
                    cohere_cache_mode=None,
                    cohere_latency_ms=None,
                    total_latency_ms=1,
                    result_count=1,
                    node_id="node-1",
                    correlation_id=None,
                )
            ]
        )

        class _Writer:
            def __init__(self, backend):
                self.backend = backend

        original = getattr(app.state, "search_event_log_writer", _SENTINEL)
        app.state.search_event_log_writer = _Writer(backend)
        try:
            resp_narrow = client.get(
                "/admin/partials/dashboard-cache-metrics?cache_window=900"
            )
            assert resp_narrow.status_code == 200
            narrow_card = next(
                c
                for c in _extract_article_cards(resp_narrow.text)
                if "<h3>On-Mode Hit Rate</h3>" in c
            )
            assert "--" in narrow_card, (
                "With a 900s window, the row 2000s old must be excluded "
                f"(placeholder '--' expected). Card HTML:\n{narrow_card[:400]}"
            )

            resp_wide = client.get(
                "/admin/partials/dashboard-cache-metrics?cache_window=3600"
            )
            assert resp_wide.status_code == 200
            wide_card = next(
                c
                for c in _extract_article_cards(resp_wide.text)
                if "<h3>On-Mode Hit Rate</h3>" in c
            )
            assert "100.0%" in wide_card, (
                "With a 3600s window, the row 2000s old must be included "
                f"(100.0% expected). Card HTML:\n{wide_card[:400]}"
            )
        finally:
            if original is _SENTINEL:
                try:
                    del app.state.search_event_log_writer
                except AttributeError:
                    pass
            else:
                app.state.search_event_log_writer = original


# ---------------------------------------------------------------------------
# Defect 2: Shadow Hit Rate must be request-denominated and windowed, same
# source as On-Mode
# ---------------------------------------------------------------------------


class TestShadowHitRateIsRequestDenominatedAndWindowed:
    def test_shadow_call_receives_from_ts_and_to_ts(
        self, client, admin_session_cookie, app
    ):
        backend = _RecordingSelBackend(
            on_result={"hits": 0, "requests": 0},
            shadow_result={"hits": 4, "requests": 5},
        )
        original = _install_sel_writer(app, backend)
        try:
            resp = client.get(
                "/admin/partials/dashboard-cache-metrics?cache_window=900"
            )
            assert resp.status_code == 200
            shadow_calls = [c for c in backend.calls if c[0] == "shadow"]
            assert len(shadow_calls) == 1, (
                f"Expected exactly one 'shadow' call, got {backend.calls}"
            )
            _, from_ts, to_ts = shadow_calls[0]
            assert from_ts is not None and to_ts is not None
            assert 850 <= (to_ts - from_ts) <= 950
        finally:
            _restore_sel_writer(app, original)

    def test_shadow_hit_rate_card_sourced_from_sel_backend_not_search_embed_event(
        self, client, admin_session_cookie, app
    ):
        """The Shadow Hit Rate card must render the sel_backend's
        get_hit_rate_counts("shadow", ...) result (4/5 = 80.0%), NOT the
        search_embed_event windowed shadow_agg (999 hits / 1000 -> 99.9%,
        deliberately absurd so any leak is unmistakable)."""
        sel_backend = _RecordingSelBackend(
            on_result={"hits": 0, "requests": 0},
            shadow_result={"hits": 4, "requests": 5},
        )
        original_sel = _install_sel_writer(app, sel_backend)
        original_see = _install_see_writer(
            app, _windowed_result_with_cosine(shadow_cosine_p50=0.97)
        )
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            shadow_card = next(
                c
                for c in _extract_article_cards(html)
                if "<h3>Shadow Hit Rate</h3>" in c
            )
            assert "80.0%" in shadow_card, (
                "Shadow Hit Rate must render 4/5=80.0% from sel_backend, got:\n"
                f"{shadow_card[:600]}"
            )
            assert "99.9%" not in shadow_card, (
                "Shadow Hit Rate must NOT render the operation-denominated "
                f"search_embed_event value:\n{shadow_card[:600]}"
            )
        finally:
            _restore_sel_writer(app, original_sel)
            _restore_see_writer(app, original_see)

    def test_shadow_cosine_cards_remain_sourced_from_search_embed_event(
        self, client, admin_session_cookie, app
    ):
        """Regression guard: the shadow COSINE cards (P50 etc.) must still
        come from search_embed_event's windowed shadow_agg, unaffected by
        the hit-rate source switch."""
        sel_backend = _RecordingSelBackend(
            on_result={"hits": 0, "requests": 0},
            shadow_result={"hits": 1, "requests": 2},
        )
        original_sel = _install_sel_writer(app, sel_backend)
        original_see = _install_see_writer(
            app, _windowed_result_with_cosine(shadow_cosine_p50=0.8642)
        )
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            assert "0.8642" in resp.text, (
                "Shadow Cosine P50 must still come from search_embed_event's "
                f"windowed result:\n{resp.text[:600]}"
            )
        finally:
            _restore_sel_writer(app, original_sel)
            _restore_see_writer(app, original_see)

    def test_shadow_hit_rate_end_to_end_request_denomination(
        self, client, admin_session_cookie, app, tmp_path
    ):
        """End-to-end (real SQLite backend, no fakes): ONE search_event_log
        row representing one request with BOTH providers in shadow mode (one
        hit, one miss) must render 100.0% (1 hit / 1 request), never 50.0%
        (which would mean the 2-operation row was miscounted as 2
        requests)."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
            SearchEventRecord,
        )

        db_path = str(tmp_path / "sel_1391_shadow_denom.db")
        backend = SearchEventLogSqliteBackend(db_path)
        backend.insert_batch(
            [
                SearchEventRecord(
                    timestamp=time.time(),
                    username="alice",
                    repo_alias="repo1",
                    search_type="semantic",
                    query_text="q",
                    voyage_cache_hit=True,
                    voyage_cache_mode="shadow",
                    voyage_latency_ms=1,
                    cohere_cache_hit=False,
                    cohere_cache_mode="shadow",
                    cohere_latency_ms=1,
                    total_latency_ms=2,
                    result_count=1,
                    node_id="node-1",
                    correlation_id=None,
                )
            ]
        )

        class _Writer:
            def __init__(self, backend):
                self.backend = backend

        original = getattr(app.state, "search_event_log_writer", _SENTINEL)
        app.state.search_event_log_writer = _Writer(backend)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            shadow_card = next(
                c
                for c in _extract_article_cards(resp.text)
                if "<h3>Shadow Hit Rate</h3>" in c
            )
            assert "100.0%" in shadow_card, (
                "One request with 2 shadow operations (1 hit) must render "
                f"100.0% (request-denominated), got:\n{shadow_card[:600]}"
            )
            assert "50.0%" not in shadow_card, (
                "Shadow Hit Rate must NOT render 50.0% -- that would mean "
                f"the row was miscounted as 2 operations:\n{shadow_card[:600]}"
            )
        finally:
            if original is _SENTINEL:
                try:
                    del app.state.search_event_log_writer
                except AttributeError:
                    pass
            else:
                app.state.search_event_log_writer = original


# ---------------------------------------------------------------------------
# Both cards fail open independently
# ---------------------------------------------------------------------------


class TestBothHitRateCardsFailOpenIndependently:
    def test_shadow_failure_does_not_suppress_on_mode_card(
        self, client, admin_session_cookie, app
    ):
        class _RaisingShadowBackend:
            def get_hit_rate_counts(self, mode, from_ts=None, to_ts=None):
                if mode == "shadow":
                    raise RuntimeError("simulated shadow failure")
                return {"hits": 3, "requests": 4}

        class _Writer:
            backend = _RaisingShadowBackend()

        original = getattr(app.state, "search_event_log_writer", _SENTINEL)
        app.state.search_event_log_writer = _Writer()
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            on_card = next(
                c
                for c in _extract_article_cards(resp.text)
                if "<h3>On-Mode Hit Rate</h3>" in c
            )
            assert "75.0%" in on_card, (
                f"On-Mode Hit Rate must still render 3/4=75.0% even when the "
                f"shadow call raises:\n{on_card[:600]}"
            )
        finally:
            if original is _SENTINEL:
                try:
                    del app.state.search_event_log_writer
                except AttributeError:
                    pass
            else:
                app.state.search_event_log_writer = original


# ---------------------------------------------------------------------------
# Documentation corrections (Bug #1391 checklist item)
# ---------------------------------------------------------------------------


class TestDocumentationCorrections:
    """The template's top-of-file comment and the Shadow Hit Rate card
    caption previously implied Shadow Hit Rate came from WindowedCacheMetrics
    / search_embed_event, same as every other non-On-Mode card. That is no
    longer true (Bug #1391) -- these must be corrected."""

    _TEMPLATE_PATH = (
        __import__("pathlib").Path(__file__).parent.parent.parent.parent.parent
        / "src/code_indexer/server/web/templates/partials/dashboard_cache_metrics.html"
    )

    def test_top_comment_mentions_shadow_hit_rate_as_search_event_log_sourced(self):
        content = self._TEMPLATE_PATH.read_text()
        # Only the leading HTML comment block, before the first <style>.
        preamble = content.split("<style>")[0]
        assert "Shadow Hit Rate" in preamble and "search_event_log" in preamble, (
            "The top-of-file comment must mention that Shadow Hit Rate (like "
            "On-Mode Hit Rate) is sourced from search_event_log, not "
            f"WindowedCacheMetrics/search_embed_event:\n{preamble}"
        )
        stale_on_mode_only_phrasing = (
            "On-Mode Hit Rate is sourced from\n     search_event_log request "
            "counts;\n     every other card is sourced from"
        )
        assert stale_on_mode_only_phrasing not in preamble, (
            "The comment must NOT still claim On-Mode Hit Rate is the ONLY "
            "card excluded from WindowedCacheMetrics/search_embed_event -- "
            f"Shadow Hit Rate is excluded too now:\n{preamble}"
        )

    def test_shadow_hit_rate_caption_states_request_denominated_search_event_log(self):
        content = self._TEMPLATE_PATH.read_text()
        idx = content.find("<h3>Shadow Hit Rate</h3>")
        assert idx >= 0
        card_html = content[idx : idx + 700]
        assert "search_event_log" in card_html, (
            "The Shadow Hit Rate card caption must state it is sourced from "
            f"search_event_log:\n{card_html}"
        )
        assert "request-denominated" in card_html, (
            "The Shadow Hit Rate card caption must state it is "
            f"request-denominated:\n{card_html}"
        )
        assert "durable" in card_html, (
            "The Shadow Hit Rate card caption must state it is durable, "
            f"matching the On-Mode Hit Rate caption's phrasing:\n{card_html}"
        )
