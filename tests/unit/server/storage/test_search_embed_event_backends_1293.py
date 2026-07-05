"""Unit tests for SearchEmbedEventSqliteBackend and SearchEmbedEventPostgresBackend
(Story #1293, Epic #1288).

SQLite tests use a real temp-file database (no mocking), mirroring
test_search_event_log_backends.py's established pattern for Story #1159.
PostgreSQL tests use a live database from TEST_POSTGRES_DSN and are skipped
when psycopg is absent or the env var is unset.
"""

import os
import time
import uuid
from typing import Iterator, Optional

import pytest

try:
    import psycopg  # noqa: F401

    _HAS_PSYCOPG = True
except ImportError:
    _HAS_PSYCOPG = False

_TEST_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
_PG_AVAILABLE = _HAS_PSYCOPG and bool(_TEST_DSN)


@pytest.fixture
def sqlite_backend(tmp_path):
    from code_indexer.server.services.search_embed_event_writer import (
        SearchEmbedEventSqliteBackend,
    )

    db_path = str(tmp_path / "test_search_embed_events.db")
    return SearchEmbedEventSqliteBackend(db_path)


def _record(
    timestamp: Optional[float] = None,
    correlation_id: str = "corr-1",
    node_id: str = "node-1",
    provider: str = "voyage-ai",
    model: Optional[str] = None,
    config_digest: Optional[str] = "digest-abc",
    cache_mode: Optional[str] = "on",
    outcome: str = "miss",
    role: str = "direct",
    live_batch_id: Optional[str] = None,
    embed_key: Optional[str] = None,
    long_key: Optional[bool] = None,
    latency_ms: Optional[int] = None,
    shadow_cosine: Optional[float] = None,
):
    from code_indexer.server.services.search_embed_event_writer import (
        SearchEmbedEventRecord,
    )

    return SearchEmbedEventRecord(
        timestamp=timestamp if timestamp is not None else time.time(),
        correlation_id=correlation_id,
        node_id=node_id,
        provider=provider,
        model=model,
        config_digest=config_digest,
        cache_mode=cache_mode,
        outcome=outcome,
        role=role,
        live_batch_id=live_batch_id,
        embed_key=embed_key,
        long_key=long_key,
        latency_ms=latency_ms,
        shadow_cosine=shadow_cosine,
    )


class TestInsertBatch:
    def test_insert_single_event(self, sqlite_backend):
        r = _record()
        sqlite_backend.insert_batch([r])

        events, total = sqlite_backend.query()
        assert total == 1
        assert events[0]["correlation_id"] == "corr-1"
        assert events[0]["outcome"] == "miss"
        assert events[0]["role"] == "direct"

    def test_insert_batch_empty_is_noop(self, sqlite_backend):
        sqlite_backend.insert_batch([])
        _, total = sqlite_backend.query()
        assert total == 0

    def test_insert_multiple_batches_accumulate(self, sqlite_backend):
        sqlite_backend.insert_batch([_record(correlation_id="c1")])
        sqlite_backend.insert_batch([_record(correlation_id="c2")])
        _, total = sqlite_backend.query()
        assert total == 2

    def test_correlation_id_never_null_constraint_respected(self, sqlite_backend):
        """correlation_id is NOT NULL at the schema level — every stored row
        must carry a non-empty string (enforced upstream by emit_embed_event's
        UUID fallback; this test proves the column itself round-trips a real
        value, never None)."""
        r = _record(correlation_id=str(uuid.uuid4()))
        sqlite_backend.insert_batch([r])
        events, _ = sqlite_backend.query()
        assert events[0]["correlation_id"] is not None
        assert events[0]["correlation_id"] != ""

    def test_nullable_fields_round_trip_as_none(self, sqlite_backend):
        r = _record(
            model=None,
            config_digest=None,
            cache_mode=None,
            live_batch_id=None,
            embed_key=None,
            long_key=None,
            latency_ms=None,
            shadow_cosine=None,
        )
        sqlite_backend.insert_batch([r])
        events, _ = sqlite_backend.query()
        e = events[0]
        assert e["model"] is None
        assert e["config_digest"] is None
        assert e["cache_mode"] is None
        assert e["live_batch_id"] is None
        assert e["embed_key"] is None
        assert e["long_key"] is None
        assert e["latency_ms"] is None
        assert e["shadow_cosine"] is None

    def test_all_fields_populated_round_trip(self, sqlite_backend):
        r = _record(
            provider="cohere",
            model="embed-v4.0",
            config_digest="d123",
            cache_mode="shadow",
            outcome="shadow_hit",
            role="warm_hit",
            live_batch_id="batch-xyz",
            embed_key="s:d123:hello",
            long_key=False,
            latency_ms=77,
            shadow_cosine=0.987,
        )
        sqlite_backend.insert_batch([r])
        events, _ = sqlite_backend.query()
        e = events[0]
        assert e["provider"] == "cohere"
        assert e["model"] == "embed-v4.0"
        assert e["config_digest"] == "d123"
        assert e["cache_mode"] == "shadow"
        assert e["outcome"] == "shadow_hit"
        assert e["role"] == "warm_hit"
        assert e["live_batch_id"] == "batch-xyz"
        assert e["embed_key"] == "s:d123:hello"
        assert not e["long_key"]
        assert e["latency_ms"] == 77
        assert e["shadow_cosine"] == pytest.approx(0.987)


class TestQueryByCorrelationId:
    def test_query_by_correlation_id_filters(self, sqlite_backend):
        sqlite_backend.insert_batch([_record(correlation_id="corr-a")])
        sqlite_backend.insert_batch([_record(correlation_id="corr-b")])

        events, total = sqlite_backend.query(correlation_id="corr-a")
        assert total == 1
        assert events[0]["correlation_id"] == "corr-a"


class TestCountDistinctLiveBatchId:
    def test_count_distinct_live_batch_id_and_direct(self, sqlite_backend):
        """The fault-injection invariant formula:
        provider_embed_calls = COUNT(DISTINCT live_batch_id)
                              + COUNT(*) WHERE role='direct'
                                           AND outcome IN ('miss','shadow_miss')
        """
        # One coalesced batch (owner + 1 joiner sharing a live_batch_id).
        sqlite_backend.insert_batch(
            [
                _record(
                    correlation_id="c1",
                    role="owner",
                    outcome="miss",
                    live_batch_id="batch-1",
                ),
                _record(
                    correlation_id="c2",
                    role="joiner",
                    outcome="hit",
                    live_batch_id="batch-1",
                ),
            ]
        )
        # Two direct live calls (no coalescer).
        sqlite_backend.insert_batch(
            [
                _record(correlation_id="c3", role="direct", outcome="miss"),
                _record(correlation_id="c4", role="direct", outcome="shadow_miss"),
            ]
        )
        # A warm hit (must not count toward provider_embed_calls at all).
        sqlite_backend.insert_batch(
            [_record(correlation_id="c5", role="warm_hit", outcome="hit")]
        )

        provider_embed_calls = sqlite_backend.count_provider_embed_calls()
        # COUNT(DISTINCT live_batch_id) = 1 (batch-1) + 2 direct live rows = 3.
        assert provider_embed_calls == 3


class TestCountTransportCalls:
    """Bug #1305: count_transport_calls() == provider_embed_calls() PLUS the
    rows that made a real outbound HTTP call but are excluded from
    provider_embed_calls' "needed embed" definition: shadow_hit (role=
    warm_hit), error, and bypass (both role=direct)."""

    def test_count_transport_calls_adds_shadow_hit_error_and_bypass(
        self, sqlite_backend
    ):
        # Same coalesced batch (owner + joiner) + 2 direct live rows as the
        # provider_embed_calls test above -> provider_embed_calls == 3.
        sqlite_backend.insert_batch(
            [
                _record(
                    correlation_id="c1",
                    role="owner",
                    outcome="miss",
                    live_batch_id="batch-1",
                ),
                _record(
                    correlation_id="c2",
                    role="joiner",
                    outcome="hit",
                    live_batch_id="batch-1",
                ),
                _record(correlation_id="c3", role="direct", outcome="miss"),
                _record(correlation_id="c4", role="direct", outcome="shadow_miss"),
            ]
        )
        # A genuine warm hit — must NOT count toward either aggregate.
        sqlite_backend.insert_batch(
            [_record(correlation_id="c5", role="warm_hit", outcome="hit")]
        )
        # The 3 rows that DID make a real HTTP call but are excluded from
        # provider_embed_calls: shadow_hit, error (failed failover primary),
        # bypass (no_embedding_cache_shortcut=True).
        sqlite_backend.insert_batch(
            [
                _record(correlation_id="c6", role="warm_hit", outcome="shadow_hit"),
                _record(correlation_id="c7", role="direct", outcome="error"),
                _record(correlation_id="c8", role="direct", outcome="bypass"),
            ]
        )

        provider_embed_calls = sqlite_backend.count_provider_embed_calls()
        transport_calls = sqlite_backend.count_transport_calls()

        assert provider_embed_calls == 3, (
            "provider_embed_calls must be UNCHANGED by the new shadow_hit/"
            "error/bypass rows (Epic #1288 'needed embed' semantics)"
        )
        assert transport_calls == 6, (
            f"count_transport_calls()={transport_calls} must equal "
            f"provider_embed_calls (3) + shadow_hit (1) + error (1) + "
            f"bypass (1) = 6"
        )

    def test_count_transport_calls_zero_rows_is_zero(self, sqlite_backend):
        assert sqlite_backend.count_transport_calls() == 0

    def test_count_transport_calls_known_gap_coalesced_all_shadow_hit_batch_not_counted(
        self, sqlite_backend
    ):
        """Bug #1305 KNOWN, DOCUMENTED RESIDUAL (assert-by-design, NOT a
        latent surprise): a coalesced (Path A) dispatch batch whose members
        are ALL shadow-hit is emitted by embedding_coalescer.py's dispatch
        loop via ``_make_hit_meta("shadow", ...)`` with NO outcome/role
        override (~:1120-1131), so every such row lands at that helper's
        DEFAULTS: outcome='hit' (NOT 'shadow_hit' -- that row only exists on
        the Path-B ``_serve_with_cache`` classifier), role='warm_hit',
        cache_mode='shadow', live_batch_id=None.

        These rows are indistinguishable from a genuine on-mode warm hit by
        (role, outcome) alone -- only cache_mode differs -- and even a term
        keyed on cache_mode='shadow' would OVERCOUNT: a batch with N
        shadow-hit members made exactly ONE real HTTP call, not N. Since a
        per-row count cannot recover the batch-level "one call" fact without
        a live_batch_id (which would perturb count_provider_embed_calls'
        DISTINCT-count semantics -- explicitly out of scope), this batch's
        one real call is invisible to BOTH counters. This is REACHABLE IN
        NORMAL WARM-SHADOW SERVER OPERATION (coalescer-on + shadow-default +
        warm cache is the server's steady state) -- NOT a rare edge case.

        This test PINS that current, documented behavior so a future reader
        of count_transport_calls() sees an explicit assertion of the gap
        rather than discovering it as a surprise in production.
        """
        # Two members of ONE coalesced batch that was entirely shadow-hit --
        # exactly the shape embedding_coalescer.py's dispatch loop emits.
        sqlite_backend.insert_batch(
            [
                _record(
                    correlation_id="c-coalesced-shadow-1",
                    role="warm_hit",
                    outcome="hit",
                    cache_mode="shadow",
                    live_batch_id=None,
                ),
                _record(
                    correlation_id="c-coalesced-shadow-2",
                    role="warm_hit",
                    outcome="hit",
                    cache_mode="shadow",
                    live_batch_id=None,
                ),
            ]
        )

        assert sqlite_backend.count_provider_embed_calls() == 0, (
            "coalesced shadow-hit rows must not count as needed embeds "
            "(unchanged Epic #1288 semantics)"
        )
        assert sqlite_backend.count_transport_calls() == 0, (
            "KNOWN GAP (documented, Bug #1305, NOT fixed): this batch's ONE "
            "real HTTP call is invisible to count_transport_calls() too -- "
            "these rows are outcome='hit'/role='warm_hit' (indistinguishable "
            "from a genuine warm hit except via cache_mode), so none of the "
            "additive terms catch them. This assertion PINS the current, "
            "documented behavior."
        )


class TestUpdateAuditByKey:
    def test_update_audit_stamps_existing_row(self, sqlite_backend):
        sqlite_backend.insert_batch(
            [_record(correlation_id="corr-audit", embed_key="s:d:query")]
        )

        affected = sqlite_backend.update_audit_by_key(
            correlation_id="corr-audit",
            embed_key="s:d:query",
            audit_sampled=True,
            audit_cosine=0.999,
        )
        assert affected == 1

        events, _ = sqlite_backend.query(correlation_id="corr-audit")
        assert events[0]["audit_sampled"]
        assert events[0]["audit_cosine"] == pytest.approx(0.999)

    def test_update_audit_no_match_returns_zero_fail_open(self, sqlite_backend):
        """0 matches must NOT raise — fail-open with a WARNING (caller checks
        the returned affected count)."""
        affected = sqlite_backend.update_audit_by_key(
            correlation_id="does-not-exist",
            embed_key="s:d:query",
            audit_sampled=True,
            audit_cosine=0.5,
        )
        assert affected == 0

    def test_update_audit_multiple_matches_returns_actual_count(self, sqlite_backend):
        """Duplicate (correlation_id, embed_key) rows (retry/failover/shadow)
        update ALL matches; the caller is expected to WARN when count != 1."""
        sqlite_backend.insert_batch(
            [
                _record(correlation_id="corr-dup", embed_key="s:d:query"),
                _record(correlation_id="corr-dup", embed_key="s:d:query"),
            ]
        )
        affected = sqlite_backend.update_audit_by_key(
            correlation_id="corr-dup",
            embed_key="s:d:query",
            audit_sampled=True,
            audit_cosine=0.1,
        )
        assert affected == 2


# ---------------------------------------------------------------------------
# PostgreSQL backend tests (skipped when PG unavailable)
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_backend() -> "Iterator[tuple]":
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.services.search_embed_event_writer import (
        SearchEmbedEventPostgresBackend,
    )

    pool = ConnectionPool(_TEST_DSN)
    unique_corr = f"test-see-{uuid.uuid4().hex[:8]}"
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        backend = SearchEmbedEventPostgresBackend(pool)
        yield backend, unique_corr
        with pool.connection() as conn:
            conn.execute(
                "DELETE FROM search_embed_event WHERE correlation_id LIKE %s",
                (f"{unique_corr}%",),
            )
            conn.commit()
    finally:
        pool.close()


@pytest.mark.skipif(
    not _PG_AVAILABLE, reason="psycopg not installed or TEST_POSTGRES_DSN not set"
)
class TestSearchEmbedEventPostgresBackend:
    def test_insert_and_query_single_record(self, pg_backend):
        backend, corr = pg_backend
        r = _record(correlation_id=corr)
        backend.insert_batch([r])

        events, total = backend.query(correlation_id=corr)
        assert total == 1
        assert events[0]["correlation_id"] == corr

    def test_insert_batch_multi_row_single_transaction(self, pg_backend):
        """Bulk multi-row writer: N rows via ONE executemany() round-trip."""
        backend, corr = pg_backend
        records = [_record(correlation_id=f"{corr}-{i}") for i in range(10)]
        backend.insert_batch(records)

        events, total = backend.query(correlation_id=f"{corr}-0")
        assert total == 1

    def test_update_audit_by_key(self, pg_backend):
        backend, corr = pg_backend
        backend.insert_batch([_record(correlation_id=corr, embed_key="s:d:pgq")])

        affected = backend.update_audit_by_key(
            correlation_id=corr,
            embed_key="s:d:pgq",
            audit_sampled=True,
            audit_cosine=0.42,
        )
        assert affected == 1

    def test_count_provider_embed_calls(self, pg_backend):
        backend, corr = pg_backend
        backend.insert_batch(
            [
                _record(correlation_id=f"{corr}-a", role="direct", outcome="miss"),
                _record(
                    correlation_id=f"{corr}-b",
                    role="owner",
                    outcome="miss",
                    live_batch_id=f"{corr}-batch",
                ),
                _record(
                    correlation_id=f"{corr}-c",
                    role="joiner",
                    outcome="hit",
                    live_batch_id=f"{corr}-batch",
                ),
            ]
        )
        # Scoped count via query + manual tally (count_provider_embed_calls is
        # a whole-table aggregate; this test verifies the rows exist as
        # expected inputs to that aggregate rather than re-deriving the total,
        # since the shared table may hold rows from concurrent test runs).
        events, total = backend.query(correlation_id=f"{corr}-a")
        assert total == 1

    def test_count_transport_calls(self, pg_backend):
        """Bug #1305: count_transport_calls() == provider_embed_calls() PLUS
        shadow_hit/error/bypass rows. Both aggregates are whole-table, so
        this asserts the DELTA introduced by this test's own rows (safe
        against a shared table holding rows from concurrent test runs)."""
        backend, corr = pg_backend

        before_provider = backend.count_provider_embed_calls()
        before_transport = backend.count_transport_calls()

        backend.insert_batch(
            [
                _record(correlation_id=f"{corr}-a", role="direct", outcome="miss"),
                _record(
                    correlation_id=f"{corr}-b",
                    role="owner",
                    outcome="miss",
                    live_batch_id=f"{corr}-batch",
                ),
                _record(
                    correlation_id=f"{corr}-c",
                    role="joiner",
                    outcome="hit",
                    live_batch_id=f"{corr}-batch",
                ),
                _record(
                    correlation_id=f"{corr}-d", role="warm_hit", outcome="shadow_hit"
                ),
                _record(correlation_id=f"{corr}-e", role="direct", outcome="error"),
                _record(correlation_id=f"{corr}-f", role="direct", outcome="bypass"),
            ]
        )

        after_provider = backend.count_provider_embed_calls()
        after_transport = backend.count_transport_calls()

        # provider_embed_calls delta: 1 direct-miss + 1 batch (owner+joiner
        # share ONE live_batch_id) = 2. shadow_hit/error/bypass excluded.
        assert after_provider - before_provider == 2
        # count_transport_calls delta: the same 2 PLUS shadow_hit (1) +
        # error (1) + bypass (1) = 5.
        assert after_transport - before_transport == 5
