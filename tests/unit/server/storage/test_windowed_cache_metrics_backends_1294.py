"""Unit tests for get_windowed_metrics() on SearchEmbedEventSqliteBackend and
SearchEmbedEventPostgresBackend (Story #1294, Epic #1288).

Mirrors test_search_embed_event_backends_1293.py's established pattern:
SQLite tests use a real temp-file database; PostgreSQL tests use a live
database from TEST_POSTGRES_DSN and are skipped when psycopg is absent or the
env var is unset.

These are the backend-integration counterpart to
test_windowed_cache_metrics_1294.py (which proves the pure aggregation
formulas in isolation) — here we prove the SAME formulas reconcile with hand
counts when read back through a REAL SQL round-trip on both backends, and
that a narrowed time window (AC2) and a backend fault (fail-open) behave
correctly.
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

    db_path = str(tmp_path / "test_windowed_cache_metrics.db")
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
    audit_sampled: Optional[bool] = None,
    audit_cosine: Optional[float] = None,
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
        audit_sampled=audit_sampled,
        audit_cosine=audit_cosine,
    )


class TestSqliteWindowedMetrics:
    def test_whole_run_window_reconciles_with_hand_counts(self, sqlite_backend):
        """AC1: a window covering the entire run reconciles hits/misses/
        provider_embed_calls/batches/texts_coalesced/dedup with hand counts.
        """
        base = 1_000_000.0
        sqlite_backend.insert_batch(
            [
                _record(
                    timestamp=base + 1,
                    role="owner",
                    outcome="miss",
                    live_batch_id="batch-1",
                    embed_key="k1",
                ),
                _record(
                    timestamp=base + 2,
                    role="joiner",
                    outcome="hit",
                    live_batch_id="batch-1",
                    embed_key="k1",
                ),
                _record(
                    timestamp=base + 3,
                    role="joiner",
                    outcome="hit",
                    live_batch_id="batch-1",
                    embed_key="k2",
                ),
                _record(timestamp=base + 4, role="direct", outcome="miss"),
                _record(timestamp=base + 5, role="warm_hit", outcome="hit"),
            ]
        )

        result = sqlite_backend.get_windowed_metrics(base, base + 1000)
        overall = result.overall

        assert overall.hits == 3  # 2 joiners + 1 warm_hit
        assert overall.misses == 2  # owner + direct
        assert overall.batches == 1
        assert overall.texts_coalesced == 3
        assert overall.dedup == 1  # 3 - 2 unique keys
        assert overall.provider_embed_calls == 1 + 1  # 1 batch + 1 direct miss

    def test_narrow_window_excludes_out_of_range_events(self, sqlite_backend):
        """AC2: a narrowed window counts only in-window events; excluded events
        must not contribute to any aggregate.
        """
        base = 2_000_000.0
        sqlite_backend.insert_batch(
            [
                _record(timestamp=base, role="direct", outcome="miss"),  # before window
                _record(
                    timestamp=base + 100, role="direct", outcome="miss"
                ),  # in window
                _record(
                    timestamp=base + 200, role="direct", outcome="hit"
                ),  # in window
            ]
        )

        result = sqlite_backend.get_windowed_metrics(base + 50, base + 300)
        overall = result.overall
        assert overall.misses == 1
        assert overall.hits == 1
        # The first (out-of-window) row must not be counted.
        assert overall.misses + overall.hits == 2

    def test_group_by_cache_mode_and_provider(self, sqlite_backend):
        base = 3_000_000.0
        sqlite_backend.insert_batch(
            [
                _record(
                    timestamp=base + 1,
                    cache_mode="on",
                    provider="voyage-ai",
                    outcome="hit",
                ),
                _record(
                    timestamp=base + 2,
                    cache_mode="shadow",
                    provider="cohere",
                    outcome="shadow_miss",
                    shadow_cosine=0.9,
                ),
            ]
        )
        result = sqlite_backend.get_windowed_metrics(base, base + 100)
        assert result.by_group[("on", "voyage-ai")].hits == 1
        assert result.by_group[("shadow", "cohere")].misses == 1
        assert result.by_cache_mode["shadow"].shadow_cosine_min == 0.9

    def test_fail_open_on_backend_error_returns_empty(self, tmp_path):
        """A corrupt/unreadable DB file must not raise — returns empty aggregates."""
        from code_indexer.server.services.search_embed_event_writer import (
            SearchEmbedEventSqliteBackend,
        )

        # Point at a path that cannot be a valid sqlite DB (a directory).
        bad_dir = tmp_path / "not_a_db_dir"
        bad_dir.mkdir()
        backend = SearchEmbedEventSqliteBackend.__new__(SearchEmbedEventSqliteBackend)
        backend._db_path = str(bad_dir)  # type: ignore[attr-defined]

        result = backend.get_windowed_metrics(0.0, time.time() + 1000)
        assert result.overall.hits == 0
        assert result.overall.hit_rate == 0.0
        assert result.by_group == {}
        assert result.by_cache_mode == {}

    def test_warm_hit_burst_within_window_adds_zero_provider_calls(
        self, sqlite_backend
    ):
        """AC3 (backend round-trip): a burst of warm hits contributes zero to
        provider_embed_calls but each counts toward hit_rate.
        """
        base = 4_000_000.0
        sqlite_backend.insert_batch(
            [
                _record(timestamp=base + i, role="warm_hit", outcome="hit")
                for i in range(1, 21)
            ]
        )
        result = sqlite_backend.get_windowed_metrics(base, base + 100)
        assert result.overall.hits == 20
        assert result.overall.provider_embed_calls == 0


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
    unique_corr = f"test-wcm-{uuid.uuid4().hex[:8]}"
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
class TestPostgresWindowedMetrics:
    def test_whole_run_window_reconciles_with_hand_counts(self, pg_backend):
        backend, corr = pg_backend
        # Unique, far-future timestamp window per test run to avoid collision
        # with concurrent test runs sharing this table (mirrors the project's
        # documented flake-avoidance pattern for the shared PG test DB).
        base = 5_000_000_000.0 + (uuid.uuid4().int % 1_000_000)

        backend.insert_batch(
            [
                _record(
                    timestamp=base + 1,
                    correlation_id=f"{corr}-a",
                    role="owner",
                    outcome="miss",
                    live_batch_id=f"{corr}-batch",
                    embed_key="k1",
                ),
                _record(
                    timestamp=base + 2,
                    correlation_id=f"{corr}-b",
                    role="joiner",
                    outcome="hit",
                    live_batch_id=f"{corr}-batch",
                    embed_key="k1",
                ),
                _record(
                    timestamp=base + 3,
                    correlation_id=f"{corr}-c",
                    role="direct",
                    outcome="miss",
                ),
            ]
        )

        result = backend.get_windowed_metrics(base, base + 100)
        overall = result.overall
        assert overall.hits == 1
        assert overall.misses == 2
        assert overall.batches == 1
        assert overall.texts_coalesced == 2
        assert overall.dedup == 1  # 2 texts, 1 unique key (k1 shared) -> dedup 1
        assert overall.provider_embed_calls == 1 + 1

    def test_narrow_window_excludes_out_of_range_events(self, pg_backend):
        backend, corr = pg_backend
        base = 5_100_000_000.0 + (uuid.uuid4().int % 1_000_000)
        backend.insert_batch(
            [
                _record(
                    timestamp=base,
                    correlation_id=f"{corr}-a",
                    role="direct",
                    outcome="miss",
                ),
                _record(
                    timestamp=base + 500,
                    correlation_id=f"{corr}-b",
                    role="direct",
                    outcome="hit",
                ),
            ]
        )
        result = backend.get_windowed_metrics(base + 250, base + 1000)
        assert result.overall.hits == 1
        assert result.overall.misses == 0

    def test_fail_open_on_backend_error_returns_empty(self):
        """A closed/invalid pool must not raise — returns empty aggregates."""
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.services.search_embed_event_writer import (
            SearchEmbedEventPostgresBackend,
        )

        # Bogus DSN — connection acquisition will fail.
        pool = ConnectionPool("postgresql://invalid:invalid@127.0.0.1:1/nonexistent")
        backend = SearchEmbedEventPostgresBackend.__new__(
            SearchEmbedEventPostgresBackend
        )
        backend._pool = pool  # type: ignore[attr-defined]

        result = backend.get_windowed_metrics(0.0, time.time() + 1000)
        assert result.overall.hits == 0
        assert result.by_group == {}
        assert result.by_cache_mode == {}
