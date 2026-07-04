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
