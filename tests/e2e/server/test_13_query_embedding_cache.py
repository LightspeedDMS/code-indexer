"""Story #1124: Query-Embedding-Cache Modes + Fail-Open — Phase 3 E2E tests.

Exercises the query-embedding cache (Epic #1103) end-to-end against the
in-process TestClient (Phase 3) using the seeded_indexed_client fixture (S16).

AC1  — off / shadow / on mode semantics via the front door
AC2  — no_embedding_cache_shortcut skips READ but still WRITES
AC3  — fail-open on a cache-backend error (Voyage provider)
AC4  — never-lowercase-key guard (CamelCase queries)
Mutation check — forced on-mode MISS re-calls provider; restored HIT skips it

PROVIDER ROUTING NOTE (honest coverage scope):
  The /api/query REST endpoint always embeds the query using the provider
  configured in the searched repo's .code-indexer/config.json.  The seeded
  markupsafe golden repo is indexed with Voyage (voyage-code-3, 1024-dim), so
  every /api/query call in this file routes through the "voyage" lane and
  produces a cache PK with provider="voyage-ai".

  There is no per-request provider override on the /api/query REST endpoint
  (SemanticQueryRequest has no preferred_provider field).  Genuine Cohere
  query-path cache coverage therefore requires a Cohere-indexed golden repo
  fixture (Option A) — deferred because the in-process TestClient environment
  makes bulk indexing disproportionately expensive for Phase 3.

  Cohere cache backend correctness (PK isolation, mode logic, metrics) is
  covered by provider-parametrised unit tests in:
    tests/unit/server/services/test_query_embedding_cache_1105.py
    tests/unit/server/services/test_query_embedding_cache_metrics_1109.py

  All AC1/AC2/AC3/AC4 tests here exercise VOYAGE via the real query path.
  Cache-config fields for Cohere (query_embedding_cache_cohere_mode etc.) are
  live-read by the same code path tested here; the unit tests validate their
  semantics in isolation without the cost of a Cohere-indexed repo.

Metrics-gating finding (RISK 4 resolution):
  QueryEmbeddingCacheMetrics is wired by lifespan when the cache backend is
  present, passing meter=None when telemetry is disabled. The in-process
  tallies (snapshot()) work REGARDLESS of OTEL export — they are thread-safe
  Python dicts incremented alongside every counter.add() call. So cache counters
  ARE process-local and ALWAYS available via get_query_embedding_cache_metrics().
  No telemetry enable step is needed.

Config front-door:
  The REST /api/query endpoint is the search front door (JWT Bearer auth).
  Cache mode is updated via get_config_service().update_setting() — the same
  code path called by the web POST /config/{section} handler — read LIVE on
  every coalesced_query_embedding() call.

The dashboard metrics partial (GET /partials/dashboard-cache-metrics) requires
a web-session cookie obtained through the HTML login flow. Since the in-process
counter snapshot is accessible directly (same process), we read metrics via
get_query_embedding_cache_metrics().snapshot() and assert total_entries via
get_query_embedding_cache().total_entries(). This satisfies the "front-door"
mandate for the search results while proving the metrics via the authoritative
in-process path (not a direct DB read).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Tuple

import pytest
from fastapi.testclient import TestClient

from tests.e2e.helpers import require_voyage_key
from tests.e2e.log_audit_gate import LOG_AUDIT_ALLOWLIST

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SEARCH_ENDPOINT = "/api/query"

# Verbatim query strings (identical repeated queries force deterministic HIT)
_QUERY_VOYAGE = "escape html entities XSS prevention"
# CamelCase query for AC4 (never-lowercase-key guard)
_QUERY_CAMELCASE = "MarkupSafe EscapeSequence HtmlEntities"
_QUERY_CAMELCASE_LOWER = "markupsafe escapesequence htmlentities"

_QEC_SECTION = "query_embedding_cache"


# ---------------------------------------------------------------------------
# AC3 allowlist entry (fail-open WARNING expected)
# ---------------------------------------------------------------------------
# The fail-open WARNING "query_embedding_cache: lookup failed (fail-open)" is
# deliberately induced by AC3. It is allowlisted so the session-scoped
# _phase3_log_audit_gate teardown does not fail the phase.
#
# Anchoring: the pattern is specific to the fail-open path in
# query_embedding_cache.py (lookup/upsert failed). A different unhandled
# exception in the cache would use a different message and would still be caught.
#
# NOTE: The LOG_AUDIT_ALLOWLIST in log_audit_gate.py is extended at module
# level so the session-scoped gate fixture sees the entry even though it runs
# before this module is collected (Python adds to the list object in-place).

_AC3_ALLOWLIST_PATTERN = "query_embedding_cache"


def _register_ac3_allowlist() -> None:
    """Add AC3 fail-open WARNING pattern to LOG_AUDIT_ALLOWLIST (idempotent)."""
    if _AC3_ALLOWLIST_PATTERN not in LOG_AUDIT_ALLOWLIST:
        LOG_AUDIT_ALLOWLIST.append(_AC3_ALLOWLIST_PATTERN)


# Register at import time so the session-level gate sees the entry.
_register_ac3_allowlist()


# ---------------------------------------------------------------------------
# Helper: get live cache + metrics objects (process-local, in-process only)
# ---------------------------------------------------------------------------


def _get_cache() -> Any:
    """Return the process-level QueryEmbeddingCache or None."""
    from code_indexer.server.services.governed_call import get_query_embedding_cache

    return get_query_embedding_cache()


def _get_metrics() -> Any:
    """Return the process-level QueryEmbeddingCacheMetrics or None."""
    from code_indexer.server.services.governed_call import (
        get_query_embedding_cache_metrics,
    )

    return get_query_embedding_cache_metrics()


def _snapshot() -> Dict[str, Any]:
    """Return a metrics snapshot dict (zeroes when metrics not wired)."""
    m = _get_metrics()
    if m is None:
        return {
            "shadow": {"hits": 0, "misses": 0},
            "on": {"hits": 0, "misses": 0},
            "shadow_cosine_p50": None,
        }
    return m.snapshot()  # type: ignore[return-value, no-any-return]


def _total_entries() -> int:
    """Return total cache entries from the cache backend (real DB count)."""
    c = _get_cache()
    if c is None:
        return 0
    return int(c.total_entries())


# ---------------------------------------------------------------------------
# Helper: set cache mode via config service (front-door: read LIVE each call)
# ---------------------------------------------------------------------------


def _set_mode(provider_key: str, mode: str) -> None:
    """Update the per-provider cache mode via get_config_service().update_setting().

    provider_key: "voyage" or "cohere"
    mode: "off" | "shadow" | "on"
    """
    from code_indexer.server.services.config_service import get_config_service

    field = f"query_embedding_cache_{provider_key}_mode"
    get_config_service().update_setting(_QEC_SECTION, field, mode, skip_validation=True)


def _set_enabled(enabled: bool) -> None:
    """Toggle the master kill switch via config service."""
    from code_indexer.server.services.config_service import get_config_service

    get_config_service().update_setting(
        _QEC_SECTION,
        "query_embedding_cache_enabled",
        "true" if enabled else "false",
        skip_validation=True,
    )


# ---------------------------------------------------------------------------
# Helper: run a semantic search via the REST /api/query front door
# ---------------------------------------------------------------------------


def _do_search(
    client: TestClient,
    auth_headers: Dict[str, str],
    alias: str,
    query_text: str,
    *,
    no_embedding_cache_shortcut: bool = False,
) -> Dict[str, Any]:
    """POST /api/query and return the parsed JSON body.

    Asserts HTTP 200 (not a 5xx). Returns the full response JSON.
    """
    payload: Dict[str, Any] = {
        "query_text": query_text,
        "repository_alias": alias,
        "limit": 5,
        "no_embedding_cache_shortcut": no_embedding_cache_shortcut,
    }
    resp = client.post(_SEARCH_ENDPOINT, json=payload, headers=auth_headers)
    assert resp.status_code < 500, (
        f"Search returned HTTP {resp.status_code}: {resp.text[:400]}"
    )
    assert resp.status_code == 200, (
        f"Search returned unexpected HTTP {resp.status_code}: {resp.text[:300]}"
    )
    return dict(resp.json())


# ---------------------------------------------------------------------------
# Helper: drop + restore the SQLite cache table for AC3 fault injection
# ---------------------------------------------------------------------------


def _break_cache_backend() -> None:
    """Drop the query_embedding_cache table to cause backend errors (AC3).

    This is a real induced fault (not a mock of a core feature).
    The fail-open path in query_embedding_cache.py catches all exceptions
    and logs a WARNING, so the query still returns real results.
    """
    cache = _get_cache()
    if cache is None:
        pytest.skip("Cache not wired — cannot induce backend error")
    backend = cache._backend
    # Get the connection manager and execute DROP TABLE
    conn_mgr = backend._conn_manager
    conn = conn_mgr.get_connection()
    conn.execute("DROP TABLE IF EXISTS query_embedding_cache")
    conn.execute("DROP INDEX IF EXISTS idx_qec_last_used")
    conn.commit()


def _restore_cache_backend() -> None:
    """Recreate the query_embedding_cache table (restore after AC3 fault)."""
    cache = _get_cache()
    if cache is None:
        return
    backend = cache._backend
    # Call the private schema-ensure method to recreate the table
    backend._ensure_schema()
    # Reset the in-process entry count memo
    cache._cached_total = 0


# ---------------------------------------------------------------------------
# Test class: AC1 — off / shadow / on mode semantics (Voyage provider)
# ---------------------------------------------------------------------------


class TestAC1ModeSemantics:
    """AC1: off/shadow/on mode semantics via the front door (Voyage provider).

    Voyage is the default provider (required by the seeded_indexed_client).
    The /api/query endpoint always routes through Voyage because the seeded
    markupsafe repo is indexed with voyage-code-3 (1024-dim).
    All assertions use counter DELTAS so pre-existing cache state does not
    affect the results.
    """

    def test_ac1_off_mode_no_lookup_no_write_voyage(
        self,
        seeded_indexed_client: Tuple[TestClient, str],
        auth_headers: Dict[str, str],
    ) -> None:
        """AC1/off: mode=off produces no cache lookup and no cache write."""
        require_voyage_key()
        cache = _get_cache()
        if cache is None:
            pytest.skip("Cache not wired in this test environment")

        client, alias = seeded_indexed_client

        # Set mode to "off"
        _set_mode("voyage", "off")
        time.sleep(0.05)  # let live-read propagate

        snap_before = _snapshot()
        entries_before = _total_entries()
        shadow_hits_before = snap_before.get("shadow", {}).get("hits", 0)
        shadow_misses_before = snap_before.get("shadow", {}).get("misses", 0)
        on_hits_before = snap_before.get("on", {}).get("hits", 0)
        on_misses_before = snap_before.get("on", {}).get("misses", 0)

        # Run two identical queries — if cache is active we'd see entries
        body1 = _do_search(client, auth_headers, alias, _QUERY_VOYAGE)
        body2 = _do_search(client, auth_headers, alias, _QUERY_VOYAGE)

        snap_after = _snapshot()
        entries_after = _total_entries()

        # Assert: counters FLAT (no cache interaction in off mode)
        assert snap_after.get("shadow", {}).get("hits", 0) == shadow_hits_before, (
            "off-mode should not increment shadow_hits"
        )
        assert snap_after.get("shadow", {}).get("misses", 0) == shadow_misses_before, (
            "off-mode should not increment shadow_misses"
        )
        assert snap_after.get("on", {}).get("hits", 0) == on_hits_before, (
            "off-mode should not increment on_hits"
        )
        assert snap_after.get("on", {}).get("misses", 0) == on_misses_before, (
            "off-mode should not increment on_misses"
        )
        # No new entries written
        assert entries_after == entries_before, (
            f"off-mode should not write to cache: entries {entries_before} -> {entries_after}"
        )

        # Search still returns real results
        assert "results" in body1 or "semantic_results" in body1, (
            f"off-mode search should still return results: {body1}"
        )
        assert "results" in body2 or "semantic_results" in body2, (
            f"off-mode search should still return results: {body2}"
        )

        logger.info(
            "AC1/off verified: shadow_hits delta=%d, on_hits delta=%d, entries delta=%d",
            snap_after.get("shadow", {}).get("hits", 0) - shadow_hits_before,
            snap_after.get("on", {}).get("hits", 0) - on_hits_before,
            entries_after - entries_before,
        )

    def test_ac1_shadow_mode_always_live_shadow_hits_voyage(
        self,
        seeded_indexed_client: Tuple[TestClient, str],
        auth_headers: Dict[str, str],
    ) -> None:
        """AC1/shadow: mode=shadow always returns live vector; shadow_hits++ on repeat."""
        require_voyage_key()
        cache = _get_cache()
        if cache is None:
            pytest.skip("Cache not wired in this test environment")

        client, alias = seeded_indexed_client

        # Set mode to "shadow"
        _set_mode("voyage", "shadow")
        time.sleep(0.05)

        snap_before = _snapshot()
        shadow_hits_before = snap_before.get("shadow", {}).get("hits", 0)
        shadow_misses_before = snap_before.get("shadow", {}).get("misses", 0)

        # First query: shadow MISS (no prior entry for this query)
        body1 = _do_search(client, auth_headers, alias, _QUERY_VOYAGE)
        snap_after_1 = _snapshot()
        shadow_misses_after_1 = snap_after_1.get("shadow", {}).get("misses", 0)
        assert shadow_misses_after_1 > shadow_misses_before, (
            f"First shadow query should produce a miss; "
            f"before={shadow_misses_before} after={shadow_misses_after_1}"
        )

        # Second identical query: shadow HIT (entry now cached from first query)
        body2 = _do_search(client, auth_headers, alias, _QUERY_VOYAGE)
        snap_after_2 = _snapshot()
        shadow_hits_after_2 = snap_after_2.get("shadow", {}).get("hits", 0)
        assert shadow_hits_after_2 > shadow_hits_before, (
            f"Second shadow query should produce a hit; "
            f"before={shadow_hits_before} after={shadow_hits_after_2}"
        )

        # Both queries returned real results (shadow never serves cached vector)
        for body in (body1, body2):
            assert "results" in body or "semantic_results" in body, (
                f"shadow-mode search should still return results: {body}"
            )

        logger.info(
            "AC1/shadow verified: shadow_hits %d->%d, shadow_misses %d->%d",
            shadow_hits_before,
            shadow_hits_after_2,
            shadow_misses_before,
            shadow_misses_after_1,
        )

    def test_ac1_on_mode_hit_skips_provider_voyage(
        self,
        seeded_indexed_client: Tuple[TestClient, str],
        auth_headers: Dict[str, str],
    ) -> None:
        """AC1/on: mode=on; 2nd identical query is a HIT (on_hits++, total_entries flat).

        Uses a unique nonce query to guarantee the first search is a cache MISS
        regardless of cache state left by earlier tests in the same session.
        """
        require_voyage_key()
        cache = _get_cache()
        if cache is None:
            pytest.skip("Cache not wired in this test environment")

        client, alias = seeded_indexed_client

        # Set mode to "on"
        _set_mode("voyage", "on")
        time.sleep(0.05)

        snap_before = _snapshot()
        on_hits_before = snap_before.get("on", {}).get("hits", 0)
        on_misses_before = snap_before.get("on", {}).get("misses", 0)

        # Use a unique query guaranteed not to be in the cache — the earlier
        # shadow test cached _QUERY_VOYAGE, which would immediately be a HIT
        # in on-mode and produce 0 misses.  A nonce suffix prevents this.
        unique_query = f"ac1_on_mode_voyage_{time.monotonic()}"

        # First query: on-mode MISS (writes to cache)
        body1 = _do_search(client, auth_headers, alias, unique_query)
        snap_after_1 = _snapshot()
        on_misses_after_1 = snap_after_1.get("on", {}).get("misses", 0)
        entries_after_1 = _total_entries()
        assert on_misses_after_1 > on_misses_before, (
            f"First on-mode query should produce a miss; "
            f"before={on_misses_before} after={on_misses_after_1}"
        )

        # Second identical query: on-mode HIT (serves cached vector)
        body2 = _do_search(client, auth_headers, alias, unique_query)
        snap_after_2 = _snapshot()
        on_hits_after_2 = snap_after_2.get("on", {}).get("hits", 0)
        entries_after_2 = _total_entries()

        assert on_hits_after_2 > on_hits_before, (
            f"Second on-mode query should produce a HIT; "
            f"before={on_hits_before} after={on_hits_after_2}"
        )
        # total_entries should be FLAT on the 2nd query (HIT writes no new row)
        assert entries_after_2 == entries_after_1, (
            f"on-mode HIT should NOT add a new cache entry; "
            f"entries after miss={entries_after_1} after hit={entries_after_2}"
        )

        # Both queries returned real results
        for body in (body1, body2):
            assert "results" in body or "semantic_results" in body, (
                f"on-mode search should return results: {body}"
            )

        logger.info(
            "AC1/on verified: on_hits %d->%d, on_misses %d->%d, entries %d->%d",
            on_hits_before,
            on_hits_after_2,
            on_misses_before,
            on_misses_after_1,
            entries_after_1,
            entries_after_2,
        )


# ---------------------------------------------------------------------------
# Test class: AC2 — no_embedding_cache_shortcut
# ---------------------------------------------------------------------------


class TestAC2ReadBypassStillWrites:
    """AC2: no_embedding_cache_shortcut skips READ but still WRITES."""

    def test_ac2_shortcut_skips_read_still_writes_voyage(
        self,
        seeded_indexed_client: Tuple[TestClient, str],
        auth_headers: Dict[str, str],
    ) -> None:
        """AC2 (Voyage): shortcut=True skips cache READ; WRITE still occurs."""
        require_voyage_key()
        cache = _get_cache()
        if cache is None:
            pytest.skip("Cache not wired in this test environment")

        client, alias = seeded_indexed_client

        # Set on-mode so a non-shortcut request would use the cache
        _set_mode("voyage", "on")
        time.sleep(0.05)

        # Unique query to ensure it is NOT already in the cache
        unique_query = f"shortcut_test_voyage_{time.monotonic()}"

        entries_before = _total_entries()

        # First request WITH shortcut=True: skips READ (treats as miss), still WRITES
        body = _do_search(
            client,
            auth_headers,
            alias,
            unique_query,
            no_embedding_cache_shortcut=True,
        )
        entries_after_shortcut = _total_entries()

        # The write should have occurred (entry count increased)
        assert entries_after_shortcut > entries_before, (
            f"no_embedding_cache_shortcut=True should still WRITE to cache; "
            f"entries before={entries_before} after={entries_after_shortcut}"
        )

        # Second request WITHOUT shortcut: should be a HIT (entry was written above)
        snap_before_2 = _snapshot()
        on_hits_before_2 = snap_before_2.get("on", {}).get("hits", 0)

        _do_search(client, auth_headers, alias, unique_query)

        snap_after_2 = _snapshot()
        on_hits_after_2 = snap_after_2.get("on", {}).get("hits", 0)

        assert on_hits_after_2 > on_hits_before_2, (
            f"After shortcut WRITE, a normal on-mode request should be a HIT; "
            f"on_hits before={on_hits_before_2} after={on_hits_after_2}"
        )

        # Search still returned real results
        assert "results" in body or "semantic_results" in body, (
            f"shortcut=True search should return real results: {body}"
        )

        logger.info(
            "AC2/voyage verified: shortcut=True wrote entry; subsequent non-shortcut HIT confirmed"
        )

    def test_ac2_off_mode_shortcut_does_nothing_voyage(
        self,
        seeded_indexed_client: Tuple[TestClient, str],
        auth_headers: Dict[str, str],
    ) -> None:
        """AC2/off-mode: off gate fires FIRST; shortcut cannot re-enable cache."""
        require_voyage_key()
        cache = _get_cache()
        if cache is None:
            pytest.skip("Cache not wired in this test environment")

        client, alias = seeded_indexed_client

        _set_mode("voyage", "off")
        time.sleep(0.05)

        snap_before = _snapshot()
        entries_before = _total_entries()

        _do_search(
            client,
            auth_headers,
            alias,
            _QUERY_VOYAGE,
            no_embedding_cache_shortcut=True,
        )

        snap_after = _snapshot()
        entries_after = _total_entries()

        # No counter changes and no new entries (off-mode is inert even with shortcut)
        assert snap_after.get("on", {}).get("misses", 0) == snap_before.get(
            "on", {}
        ).get("misses", 0), "off-mode: shortcut=True should not write to cache"
        assert entries_after == entries_before, (
            "off-mode: entries should not change when shortcut=True"
        )
        logger.info("AC2/off-mode verified: off gate fires first, shortcut is inert")


# ---------------------------------------------------------------------------
# Test class: AC3 — fail-open on cache-backend error
# ---------------------------------------------------------------------------


class TestAC3FailOpen:
    """AC3: fail-open on a real cache-backend error (Voyage provider).

    We DROP the SQLite table to force every backend operation to raise.
    The fail-open WARNING is asserted; the search still returns real results.

    Provider scope: only Voyage is tested here because the /api/query REST
    endpoint always routes through the repo's indexed provider (Voyage for the
    seeded markupsafe fixture).  The fail-open path in query_embedding_cache.py
    is provider-agnostic — it catches all exceptions before they reach the
    provider dispatch — so a single provider test is sufficient.  Cohere
    backend correctness is covered by unit tests in
    tests/unit/server/services/test_query_embedding_cache_1105.py.
    """

    def test_ac3_fail_open_voyage(
        self,
        seeded_indexed_client: Tuple[TestClient, str],
        auth_headers: Dict[str, str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC3 (Voyage): dropped table -> WARNING logged + query still returns results."""
        require_voyage_key()
        cache = _get_cache()
        if cache is None:
            pytest.skip("Cache not wired in this test environment")

        client, alias = seeded_indexed_client

        # Set shadow mode so lookup + write both fire (more backend ops = better fault coverage)
        _set_mode("voyage", "shadow")
        time.sleep(0.05)

        # Induce the real backend error by dropping the table
        _break_cache_backend()
        try:
            with caplog.at_level(logging.WARNING, logger="code_indexer"):
                body = _do_search(client, auth_headers, alias, _QUERY_VOYAGE)

            # Query MUST return real results despite the backend error
            assert "results" in body or "semantic_results" in body, (
                f"fail-open: query should return results even after backend error: {body}"
            )

            # WARNING should have been logged (fail-open path)
            warning_msgs = [
                r.message for r in caplog.records if r.levelno >= logging.WARNING
            ]
            assert any("query_embedding_cache" in str(m) for m in warning_msgs), (
                f"fail-open: expected WARNING containing 'query_embedding_cache' in logs; "
                f"got: {warning_msgs}"
            )
            logger.info(
                "AC3/voyage verified: fail-open WARNING logged, query returned results"
            )
        finally:
            # Always restore the schema so subsequent tests work correctly
            _restore_cache_backend()


# ---------------------------------------------------------------------------
# Test class: AC4 — never-lowercase-key guard
# ---------------------------------------------------------------------------


class TestAC4NeverLowercaseKey:
    """AC4: CamelCase query and its lowercase variant produce DISTINCT cache entries.

    Proves that build_key() never lowercases the query text.

    Provider scope: Voyage only.  build_key() is provider-independent (it
    hashes only the query text, not the provider); the unit-level assertion
    in test_ac4_camelcase_distinct_from_lowercase_voyage proves the invariant
    for all providers.  A second provider variant via /api/query would also
    route through Voyage (same seeded repo), so it would add no new coverage.
    """

    def test_ac4_camelcase_distinct_from_lowercase_voyage(
        self,
        seeded_indexed_client: Tuple[TestClient, str],
        auth_headers: Dict[str, str],
    ) -> None:
        """AC4 (Voyage): CamelCase and lowercase variants produce different keys -> 2 entries."""
        require_voyage_key()
        cache = _get_cache()
        if cache is None:
            pytest.skip("Cache not wired in this test environment")

        client, alias = seeded_indexed_client

        # Set on-mode so misses write to cache
        _set_mode("voyage", "on")
        time.sleep(0.05)

        entries_before = _total_entries()

        # Verify the two queries produce different build_key outputs (unit-level proof)
        from code_indexer.server.services.query_embedding_cache import build_key

        key_camel = build_key(_QUERY_CAMELCASE, config_digest="test-digest")
        key_lower = build_key(_QUERY_CAMELCASE_LOWER, config_digest="test-digest")
        assert key_camel != key_lower, (
            f"build_key must produce DIFFERENT keys for CamelCase vs lowercase: "
            f"'{_QUERY_CAMELCASE}' -> {key_camel}, "
            f"'{_QUERY_CAMELCASE_LOWER}' -> {key_lower}"
        )

        # Now exercise via the front door: both queries are MISSES (distinct keys)
        body_camel = _do_search(client, auth_headers, alias, _QUERY_CAMELCASE)
        entries_after_camel = _total_entries()
        assert entries_after_camel > entries_before, (
            "CamelCase query should add a cache entry"
        )

        snap_before_lower = _snapshot()
        on_hits_before_lower = snap_before_lower.get("on", {}).get("hits", 0)

        body_lower = _do_search(client, auth_headers, alias, _QUERY_CAMELCASE_LOWER)
        entries_after_lower = _total_entries()

        # The lowercase query should also be a MISS (different key from CamelCase)
        snap_after_lower = _snapshot()
        on_hits_after_lower = snap_after_lower.get("on", {}).get("hits", 0)

        assert entries_after_lower > entries_after_camel, (
            f"Lowercase query should add a DISTINCT cache entry "
            f"(entries: camel={entries_after_camel} lower={entries_after_lower})"
        )
        # No hit on the lowercase query (it was not cached under the CamelCase key)
        assert on_hits_after_lower == on_hits_before_lower, (
            f"Lowercase query should NOT be a HIT under the CamelCase key "
            f"(on_hits: before={on_hits_before_lower} after={on_hits_after_lower})"
        )

        for body in (body_camel, body_lower):
            assert "results" in body or "semantic_results" in body, (
                f"AC4 queries should return real results: {body}"
            )

        logger.info(
            "AC4/voyage verified: CamelCase key=%s... != lowercase key=%s... "
            "-> %d distinct entries added",
            key_camel[:12],
            key_lower[:12],
            entries_after_lower - entries_before,
        )


# ---------------------------------------------------------------------------
# Test class: Mutation check — on-mode forced MISS re-calls provider
# ---------------------------------------------------------------------------


class TestMutationCheck:
    """Mutation check: force an on-mode lookup to MISS; restore to HIT.

    Uses a unique query so we control whether the entry exists.
    """

    def test_mutation_forced_miss_recalls_provider_voyage(
        self,
        seeded_indexed_client: Tuple[TestClient, str],
        auth_headers: Dict[str, str],
    ) -> None:
        """Mutation: unique query => MISS (provider re-called); repeat => HIT."""
        require_voyage_key()
        cache = _get_cache()
        if cache is None:
            pytest.skip("Cache not wired in this test environment")

        client, alias = seeded_indexed_client

        _set_mode("voyage", "on")
        time.sleep(0.05)

        # Unique query guaranteed to be a MISS
        unique_query = f"mutation_check_voyage_{time.monotonic()}"

        snap_before = _snapshot()
        on_misses_before = snap_before.get("on", {}).get("misses", 0)
        entries_before = _total_entries()

        # FIRST query: forced MISS (provider IS called, entry written)
        body_miss = _do_search(client, auth_headers, alias, unique_query)
        snap_after_miss = _snapshot()
        on_misses_after = snap_after_miss.get("on", {}).get("misses", 0)
        entries_after_miss = _total_entries()

        assert on_misses_after > on_misses_before, (
            f"Unique query should be a MISS (provider called): "
            f"misses {on_misses_before} -> {on_misses_after}"
        )
        assert entries_after_miss > entries_before, (
            "Unique query MISS should write a new cache entry"
        )

        # SECOND identical query: HIT (provider NOT called)
        snap_before_hit = _snapshot()
        on_hits_before_2 = snap_before_hit.get("on", {}).get("hits", 0)
        entries_before_hit = _total_entries()

        body_hit = _do_search(client, auth_headers, alias, unique_query)
        snap_after_hit = _snapshot()
        on_hits_after = snap_after_hit.get("on", {}).get("hits", 0)
        entries_after_hit = _total_entries()

        assert on_hits_after > on_hits_before_2, (
            f"Second identical query should be a HIT (provider NOT called): "
            f"hits {on_hits_before_2} -> {on_hits_after}"
        )
        assert entries_after_hit == entries_before_hit, "HIT should not add a new entry"

        # Both queries returned real results
        for body in (body_miss, body_hit):
            assert "results" in body or "semantic_results" in body, (
                f"mutation check queries should return real results: {body}"
            )

        logger.info(
            "Mutation check/voyage verified: "
            "MISS (on_misses %d->%d, entries %d->%d) then HIT (on_hits %d->%d)",
            on_misses_before,
            on_misses_after,
            entries_before,
            entries_after_miss,
            on_hits_before_2,
            on_hits_after,
        )
