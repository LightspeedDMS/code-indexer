"""Regression tests for GitHub issue #1257.

The dashboard "On-Mode Hit Rate" card was OPERATION-denominated: it read
in-process QueryEmbeddingCacheMetrics tallies that increment once per cache
OPERATION. The analytics search_event_log table is REQUEST-denominated: one
row per user request (written once in the request handler's `finally` block).
On any path that performs more than one on-mode embedding operation per
request (verified reproducible on the MCP activated-repo search path), the
two numbers diverge and the dashboard overstates/understates the true
per-search served-from-cache rate.

Fix: dashboard_cache_metrics_partial (src/code_indexer/server/web/routes.py)
now derives on_hits/on_requests from
SearchEventLogSqliteBackend/SearchEventLogPostgresBackend.get_hit_rate_counts
("on") -- one row = one request -- instead of the in-process operation
tallies. These tests prove the rendered percentage matches the REQUEST count,
not the (deliberately different) OPERATION count reported by a fake
QueryEmbeddingCacheMetrics installed alongside it.
"""

from __future__ import annotations

import re
import time
from typing import Optional

import pytest
from fastapi.testclient import TestClient


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
    """Form-based login — identical to test_dashboard_cache_metrics_handler_1109.py."""
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


class _FakeMetrics:
    """Minimal fake reproducing the OLD operation-denominated snapshot() shape."""

    def __init__(self, on_hits: int, on_misses: int) -> None:
        self._snap = {
            "shadow": {"hits": 0, "misses": 0},
            "on": {"hits": on_hits, "misses": on_misses},
            "shadow_cosine_p50": None,
            "audit_total": 0,
            "audit_top1_matches": 0,
            "audit_overlap_avg": None,
        }

    def snapshot(self) -> dict:
        return self._snap


class _WriterWithBackend:
    """Minimal stand-in for SearchEventLogWriter exposing only `.backend`,
    matching the `getattr(request.app.state, "search_event_log_writer", None)`
    then `.backend` access pattern used in routes.py / inline_admin_ops.py."""

    def __init__(self, backend) -> None:
        self.backend = backend


def _record(
    voyage_cache_mode: Optional[str] = None,
    voyage_cache_hit: Optional[bool] = None,
    cohere_cache_mode: Optional[str] = None,
    cohere_cache_hit: Optional[bool] = None,
):
    from code_indexer.server.services.search_event_log_writer import SearchEventRecord

    return SearchEventRecord(
        timestamp=time.time(),
        username="alice",
        repo_alias="repo1",
        search_type="semantic",
        query_text="hello world",
        voyage_cache_hit=voyage_cache_hit,
        voyage_cache_mode=voyage_cache_mode,
        voyage_latency_ms=10,
        cohere_cache_hit=cohere_cache_hit,
        cohere_cache_mode=cohere_cache_mode,
        cohere_latency_ms=None,
        total_latency_ms=20,
        result_count=5,
        node_id="node-1",
        correlation_id=None,
    )


def _extract_article_cards(html: str) -> list:
    return html.split("<article")


class TestOnModeHitRateIsRequestDenominated:
    """Core #1257 regression: On-Mode Hit Rate must match search_event_log
    REQUEST counts, not the in-process OPERATION tallies.

    Scenario: 3 search_event_log rows (3 requests), 2 of which perform BOTH
    a voyage AND a cohere on-mode operation (the exact >1-op-per-request
    pattern the issue reproduces on the MCP activated-repo path):
        row1: voyage(on,hit)  + cohere(on,miss) -> request HIT  (OR-combine)
        row2: voyage(on,miss) + cohere(on,miss) -> request MISS
        row3: voyage(on,hit)  + cohere(on,hit)  -> request HIT
    Request-denominated: 2 hits / 3 requests = 66.7%.

    The old operation-denominated tallies for this SAME underlying activity
    would be 6 operations (3 requests x 2 providers) with 3 hits (voyage-hit
    row1, voyage-hit row3, cohere-hit row3) = 3/6 = 50.0%. We install a fake
    QueryEmbeddingCacheMetrics reporting exactly that stale 50.0% alongside
    the real search_event_log rows to prove the rendered card reads from
    search_event_log, not from the fake operation tallies.
    """

    def test_on_mode_hit_rate_matches_request_count_not_operation_count(
        self, client, admin_session_cookie, tmp_path
    ):
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache_metrics,
            clear_query_embedding_cache_metrics,
        )
        from code_indexer.server.app import app as real_app

        db_path = str(tmp_path / "sel_1257.db")
        backend = SearchEventLogSqliteBackend(db_path)
        backend.insert_batch(
            [
                _record(
                    voyage_cache_mode="on",
                    voyage_cache_hit=True,
                    cohere_cache_mode="on",
                    cohere_cache_hit=False,
                ),
                _record(
                    voyage_cache_mode="on",
                    voyage_cache_hit=False,
                    cohere_cache_mode="on",
                    cohere_cache_hit=False,
                ),
                _record(
                    voyage_cache_mode="on",
                    voyage_cache_hit=True,
                    cohere_cache_mode="on",
                    cohere_cache_hit=True,
                ),
            ]
        )

        # Old operation-denominated reading for the SAME activity: 3 hits / 6 ops = 50.0%.
        set_query_embedding_cache_metrics(_FakeMetrics(on_hits=3, on_misses=3))
        original_writer = getattr(real_app.state, "search_event_log_writer", None)
        real_app.state.search_event_log_writer = _WriterWithBackend(backend)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text

            cards = _extract_article_cards(html)
            assert len(cards) >= 4, (
                f"Expected at least 3 article cards, got {len(cards) - 1}"
            )
            on_mode_card = cards[
                3
            ]  # Cache Entries, Shadow Hit Rate, On-Mode Hit Rate, ...

            assert "66.7%" in on_mode_card, (
                "On-Mode Hit Rate must be REQUEST-denominated: 2 hits / 3 "
                f"requests = 66.7%. Card HTML:\n{on_mode_card[:600]}"
            )
            assert "50.0%" not in on_mode_card, (
                "On-Mode Hit Rate must NOT render the stale OPERATION-"
                "denominated rate (3 hits / 6 ops = 50.0%) from the fake "
                f"in-process metrics tallies. Card HTML:\n{on_mode_card[:600]}"
            )
        finally:
            clear_query_embedding_cache_metrics()
            if original_writer is not None:
                real_app.state.search_event_log_writer = original_writer
            else:
                try:
                    del real_app.state.search_event_log_writer
                except AttributeError:
                    pass

    def test_on_mode_card_no_longer_claims_volatile_or_resets_on_restart(
        self, client, admin_session_cookie, tmp_path
    ):
        """The On-Mode Hit Rate card is now sourced from the durable,
        cluster-shared search_event_log table -- NOT the in-memory per-node
        operation tallies. Its copy must not claim "resets on restart" or
        "not aggregated across cluster nodes", which would now be false and
        would itself mislead operators (the exact class of bug #1257 reports).
        """
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )
        from code_indexer.server.app import app as real_app

        db_path = str(tmp_path / "sel_1257_copy.db")
        backend = SearchEventLogSqliteBackend(db_path)
        backend.insert_batch([_record(voyage_cache_mode="on", voyage_cache_hit=True)])

        original_writer = getattr(real_app.state, "search_event_log_writer", None)
        real_app.state.search_event_log_writer = _WriterWithBackend(backend)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            cards = _extract_article_cards(html)
            on_mode_card = next((c for c in cards if "On-Mode Hit Rate" in c), None)
            assert on_mode_card is not None, (
                f"On-Mode Hit Rate card not found in rendered HTML:\n{html[:600]}"
            )

            assert "resets on restart" not in on_mode_card, (
                f"On-Mode Hit Rate card must not claim 'resets on restart' "
                f"now that it is DB-backed:\n{on_mode_card[:600]}"
            )
            assert "not aggregated across cluster nodes" not in on_mode_card, (
                "On-Mode Hit Rate card must not claim 'not aggregated across "
                f"cluster nodes' now that it reads the shared table:\n{on_mode_card[:600]}"
            )
        finally:
            if original_writer is not None:
                real_app.state.search_event_log_writer = original_writer
            else:
                try:
                    del real_app.state.search_event_log_writer
                except AttributeError:
                    pass

    def test_on_mode_hit_rate_zero_requests_renders_placeholder(
        self, client, admin_session_cookie, tmp_path
    ):
        """No search_event_log rows in 'on' mode -> On-Mode Hit Rate renders
        '--' (no data), not a crash and not a stale operation-based number."""
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache_metrics,
            clear_query_embedding_cache_metrics,
        )
        from code_indexer.server.app import app as real_app

        db_path = str(tmp_path / "sel_1257_empty.db")
        backend = SearchEventLogSqliteBackend(db_path)

        set_query_embedding_cache_metrics(_FakeMetrics(on_hits=9, on_misses=1))
        original_writer = getattr(real_app.state, "search_event_log_writer", None)
        real_app.state.search_event_log_writer = _WriterWithBackend(backend)
        try:
            resp = client.get("/admin/partials/dashboard-cache-metrics")
            assert resp.status_code == 200
            html = resp.text
            cards = _extract_article_cards(html)
            on_mode_card = cards[3]

            assert "90.0%" not in on_mode_card, (
                "With zero search_event_log rows, On-Mode Hit Rate must not "
                "render the stale operation-based 90.0% (9 hits / 10 ops) "
                f"from the fake in-process metrics. Card HTML:\n{on_mode_card[:600]}"
            )
        finally:
            clear_query_embedding_cache_metrics()
            if original_writer is not None:
                real_app.state.search_event_log_writer = original_writer
            else:
                try:
                    del real_app.state.search_event_log_writer
                except AttributeError:
                    pass
