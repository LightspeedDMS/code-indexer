"""Unit tests for SearchEventLogSqliteBackend and SearchEventLogPostgresBackend
(Issue #1159).

SQLite tests use a real temp-file database (no mocking).
PostgreSQL tests use a live database from TEST_POSTGRES_DSN and are skipped
when psycopg is absent or the env var is unset.
"""

import os
import time
import uuid
from typing import Iterator, Optional

import pytest

# ---------------------------------------------------------------------------
# PostgreSQL availability gate (mirrors test_logs_postgres_alias.py pattern)
# ---------------------------------------------------------------------------

try:
    import psycopg  # noqa: F401

    _HAS_PSYCOPG = True
except ImportError:
    _HAS_PSYCOPG = False

_TEST_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
_PG_AVAILABLE = _HAS_PSYCOPG and bool(_TEST_DSN)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_backend(tmp_path):
    """Create a fresh SQLiteBackend backed by a temporary file."""
    from code_indexer.server.services.search_event_log_writer import (
        SearchEventLogSqliteBackend,
    )

    db_path = str(tmp_path / "test_search_events.db")
    return SearchEventLogSqliteBackend(db_path)


def _record(
    username: str = "alice",
    repo_alias: Optional[str] = "repo1",
    search_type: str = "semantic",
    query_text: str = "hello world",
    voyage_cache_hit: Optional[bool] = None,
    voyage_cache_mode: Optional[str] = None,
    voyage_latency_ms: Optional[int] = None,
    cohere_cache_hit: Optional[bool] = None,
    cohere_cache_mode: Optional[str] = None,
    cohere_latency_ms: Optional[int] = None,
    total_latency_ms: int = 100,
    result_count: int = 5,
    node_id: str = "node-1",
    correlation_id: Optional[str] = None,
    timestamp: Optional[float] = None,
):
    from code_indexer.server.services.search_event_log_writer import SearchEventRecord

    return SearchEventRecord(
        timestamp=timestamp if timestamp is not None else time.time(),
        username=username,
        repo_alias=repo_alias,
        search_type=search_type,
        query_text=query_text,
        voyage_cache_hit=voyage_cache_hit,
        voyage_cache_mode=voyage_cache_mode,
        voyage_latency_ms=voyage_latency_ms,
        cohere_cache_hit=cohere_cache_hit,
        cohere_cache_mode=cohere_cache_mode,
        cohere_latency_ms=cohere_latency_ms,
        total_latency_ms=total_latency_ms,
        result_count=result_count,
        node_id=node_id,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# insert_batch
# ---------------------------------------------------------------------------


class TestInsertBatch:
    def test_insert_single_event(self, sqlite_backend):
        """insert_batch with one record stores it correctly."""
        r = _record()
        sqlite_backend.insert_batch([r])

        events, total = sqlite_backend.query()
        assert total == 1
        assert len(events) == 1
        assert events[0]["username"] == "alice"
        assert events[0]["query_text"] == "hello world"

    def test_insert_500_events(self, sqlite_backend):
        """insert_batch with 500 records stores all of them."""
        records = [_record(query_text=f"query {i}") for i in range(500)]
        sqlite_backend.insert_batch(records)

        _, total = sqlite_backend.query(limit=1)
        assert total == 500

    def test_insert_batch_empty_is_noop(self, sqlite_backend):
        """insert_batch([]) does not raise and stores nothing."""
        sqlite_backend.insert_batch([])
        _, total = sqlite_backend.query()
        assert total == 0

    def test_insert_multiple_batches(self, sqlite_backend):
        """Multiple insert_batch calls accumulate records."""
        sqlite_backend.insert_batch([_record(username="u1")])
        sqlite_backend.insert_batch([_record(username="u2")])
        sqlite_backend.insert_batch([_record(username="u3")])

        _, total = sqlite_backend.query()
        assert total == 3

    def test_unique_auto_generated_ids(self, sqlite_backend):
        """Each inserted record gets a unique auto-generated id."""
        records = [_record() for _ in range(5)]
        sqlite_backend.insert_batch(records)

        events, _ = sqlite_backend.query(limit=10)
        ids = [e["id"] for e in events]
        assert len(set(ids)) == 5, f"Expected 5 unique ids, got: {ids}"


# ---------------------------------------------------------------------------
# NULL fields stored and retrieved correctly
# ---------------------------------------------------------------------------


class TestNullFields:
    def test_all_nullable_fields_as_none(self, sqlite_backend):
        """NULL optional fields round-trip correctly."""
        r = _record(
            repo_alias=None,
            voyage_cache_hit=None,
            voyage_cache_mode=None,
            voyage_latency_ms=None,
            cohere_cache_hit=None,
            cohere_cache_mode=None,
            cohere_latency_ms=None,
            correlation_id=None,
        )
        sqlite_backend.insert_batch([r])

        events, _ = sqlite_backend.query()
        e = events[0]
        assert e["repo_alias"] is None
        assert e["voyage_cache_hit"] is None
        assert e["voyage_cache_mode"] is None
        assert e["voyage_latency_ms"] is None
        assert e["cohere_cache_hit"] is None
        assert e["cohere_cache_mode"] is None
        assert e["cohere_latency_ms"] is None
        assert e["correlation_id"] is None

    def test_all_fields_populated(self, sqlite_backend):
        """All non-NULL fields round-trip correctly."""
        r = _record(
            username="bob",
            repo_alias="myrepo",
            search_type="fts",
            query_text="SELECT * FROM",
            voyage_cache_hit=True,
            voyage_cache_mode="on",
            voyage_latency_ms=42,
            cohere_cache_hit=False,
            cohere_cache_mode="shadow",
            cohere_latency_ms=88,
            total_latency_ms=250,
            result_count=3,
            node_id="node-abc",
            correlation_id="corr-xyz",
        )
        sqlite_backend.insert_batch([r])

        events, _ = sqlite_backend.query()
        e = events[0]
        assert e["username"] == "bob"
        assert e["repo_alias"] == "myrepo"
        assert e["search_type"] == "fts"
        assert e["voyage_latency_ms"] == 42
        assert e["cohere_latency_ms"] == 88
        assert e["correlation_id"] == "corr-xyz"
        assert e["node_id"] == "node-abc"

    def test_boolean_cache_hit_roundtrip(self, sqlite_backend):
        """Boolean cache hit fields store and return truthy/falsy values."""
        r = _record(voyage_cache_hit=True, cohere_cache_hit=False)
        sqlite_backend.insert_batch([r])

        events, _ = sqlite_backend.query()
        e = events[0]
        # SQLite stores booleans as 0/1; we just check they are truthy/falsy
        assert e["voyage_cache_hit"]
        assert not e["cohere_cache_hit"]


# ---------------------------------------------------------------------------
# Special characters in query_text
# ---------------------------------------------------------------------------


class TestSpecialCharacters:
    def test_sql_injection_in_query_text(self, sqlite_backend):
        """SQL special characters in query_text are stored safely."""
        evil = "'; DROP TABLE search_event_log; --"
        sqlite_backend.insert_batch([_record(query_text=evil)])

        events, total = sqlite_backend.query()
        assert total == 1
        assert events[0]["query_text"] == evil

    def test_unicode_in_query_text(self, sqlite_backend):
        """Unicode characters in query_text round-trip correctly."""
        unicode_text = "emoji 🔥 and Chinese 你好 and Arabic مرحبا"
        sqlite_backend.insert_batch([_record(query_text=unicode_text)])

        events, _ = sqlite_backend.query()
        assert events[0]["query_text"] == unicode_text

    def test_newlines_in_query_text(self, sqlite_backend):
        """Newline characters in query_text round-trip correctly."""
        multiline = "line one\nline two\ttabbed"
        sqlite_backend.insert_batch([_record(query_text=multiline)])

        events, _ = sqlite_backend.query()
        assert events[0]["query_text"] == multiline


# ---------------------------------------------------------------------------
# prune_older_than
# ---------------------------------------------------------------------------


class TestPruneOlderThan:
    def test_prune_removes_old_records(self, sqlite_backend):
        """Records older than cutoff are deleted."""
        old_ts = time.time() - 1000
        new_ts = time.time()

        sqlite_backend.insert_batch([_record(timestamp=old_ts, username="old")])
        sqlite_backend.insert_batch([_record(timestamp=new_ts, username="new")])

        # Prune records older than 500 seconds ago
        cutoff = time.time() - 500
        sqlite_backend.prune_older_than(cutoff)

        events, total = sqlite_backend.query()
        assert total == 1
        assert events[0]["username"] == "new"

    def test_prune_keeps_records_at_cutoff_boundary(self, sqlite_backend):
        """Records exactly at the cutoff timestamp are NOT pruned (half-open [cutoff, ...))."""
        cutoff = time.time() - 100
        sqlite_backend.insert_batch([_record(timestamp=cutoff, username="boundary")])
        sqlite_backend.insert_batch([_record(timestamp=cutoff + 1, username="after")])

        sqlite_backend.prune_older_than(cutoff)

        events, total = sqlite_backend.query()
        # Both should survive since timestamp >= cutoff
        assert total == 2

    def test_prune_all_records(self, sqlite_backend):
        """Pruning with a far-future cutoff removes all records."""
        for i in range(5):
            sqlite_backend.insert_batch([_record()])

        sqlite_backend.prune_older_than(time.time() + 9999)

        _, total = sqlite_backend.query()
        assert total == 0

    def test_prune_empty_table_is_noop(self, sqlite_backend):
        """Pruning an empty table does not raise."""
        sqlite_backend.prune_older_than(time.time())  # Should not raise


# ---------------------------------------------------------------------------
# query filters
# ---------------------------------------------------------------------------


class TestQueryFilters:
    def test_filter_by_username(self, sqlite_backend):
        """Querying by username returns only that user's events."""
        sqlite_backend.insert_batch([_record(username="alice")])
        sqlite_backend.insert_batch([_record(username="bob")])
        sqlite_backend.insert_batch([_record(username="alice")])

        events, total = sqlite_backend.query(username="alice")
        assert total == 2
        assert all(e["username"] == "alice" for e in events)

    def test_filter_by_search_type(self, sqlite_backend):
        """Querying by search_type returns only that type's events."""
        sqlite_backend.insert_batch([_record(search_type="semantic")])
        sqlite_backend.insert_batch([_record(search_type="fts")])
        sqlite_backend.insert_batch([_record(search_type="semantic")])

        events, total = sqlite_backend.query(search_type="semantic")
        assert total == 2
        assert all(e["search_type"] == "semantic" for e in events)

    def test_filter_by_repo_alias(self, sqlite_backend):
        """Querying by repo_alias returns only that repo's events."""
        sqlite_backend.insert_batch([_record(repo_alias="repo-a")])
        sqlite_backend.insert_batch([_record(repo_alias="repo-b")])

        events, total = sqlite_backend.query(repo_alias="repo-a")
        assert total == 1
        assert events[0]["repo_alias"] == "repo-a"

    def test_filter_combined(self, sqlite_backend):
        """Multiple filters are ANDed together."""
        sqlite_backend.insert_batch(
            [_record(username="alice", search_type="semantic", repo_alias="r1")]
        )
        sqlite_backend.insert_batch(
            [_record(username="alice", search_type="fts", repo_alias="r1")]
        )
        sqlite_backend.insert_batch(
            [_record(username="bob", search_type="semantic", repo_alias="r1")]
        )

        events, total = sqlite_backend.query(username="alice", search_type="semantic")
        assert total == 1
        assert events[0]["username"] == "alice"
        assert events[0]["search_type"] == "semantic"

    def test_no_filters_returns_all(self, sqlite_backend):
        """Query with no filters returns all records."""
        for i in range(5):
            sqlite_backend.insert_batch([_record(username=f"user{i}")])

        _, total = sqlite_backend.query()
        assert total == 5


# ---------------------------------------------------------------------------
# Half-open time range [from_ts, to_ts)
# ---------------------------------------------------------------------------


class TestTimeRangeFilter:
    def test_from_ts_inclusive(self, sqlite_backend):
        """from_ts is inclusive: records with timestamp >= from_ts are included."""
        t = 1000.0
        sqlite_backend.insert_batch([_record(timestamp=t)])
        sqlite_backend.insert_batch([_record(timestamp=t - 1)])

        events, total = sqlite_backend.query(from_ts=t)
        assert total == 1
        assert events[0]["timestamp"] == t

    def test_to_ts_exclusive(self, sqlite_backend):
        """to_ts is exclusive: records with timestamp < to_ts are included."""
        t = 1000.0
        sqlite_backend.insert_batch([_record(timestamp=t)])  # excluded
        sqlite_backend.insert_batch([_record(timestamp=t - 1)])  # included

        events, total = sqlite_backend.query(to_ts=t)
        assert total == 1
        assert events[0]["timestamp"] == t - 1

    def test_half_open_range(self, sqlite_backend):
        """Half-open range [from_ts, to_ts) correctly filters events."""
        sqlite_backend.insert_batch([_record(timestamp=100.0)])  # before
        sqlite_backend.insert_batch([_record(timestamp=200.0)])  # in range
        sqlite_backend.insert_batch([_record(timestamp=300.0)])  # in range
        sqlite_backend.insert_batch([_record(timestamp=400.0)])  # after

        events, total = sqlite_backend.query(from_ts=200.0, to_ts=400.0)
        assert total == 2
        timestamps = sorted(e["timestamp"] for e in events)
        assert timestamps == [200.0, 300.0]


# ---------------------------------------------------------------------------
# Ordering and pagination
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_results_ordered_by_timestamp_desc(self, sqlite_backend):
        """Query results are ordered by timestamp DESC (newest first)."""
        for ts in [100.0, 300.0, 200.0]:
            sqlite_backend.insert_batch([_record(timestamp=ts)])

        events, _ = sqlite_backend.query()
        timestamps = [e["timestamp"] for e in events]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_pagination_limit_and_offset(self, sqlite_backend):
        """Limit and offset work correctly for pagination."""
        for i in range(10):
            sqlite_backend.insert_batch([_record(timestamp=float(i))])

        # Get second page of 3
        events, total = sqlite_backend.query(limit=3, offset=3)
        assert total == 10  # Total count is always full count
        assert len(events) == 3

    def test_limit_default_100(self, sqlite_backend):
        """Default limit is 100 events."""
        sqlite_backend.insert_batch([_record() for _ in range(150)])

        events, total = sqlite_backend.query()
        assert total == 150
        assert len(events) == 100  # Only 100 returned by default


# ---------------------------------------------------------------------------
# node_id stored correctly
# ---------------------------------------------------------------------------


class TestNodeId:
    def test_node_id_stored_and_retrieved(self, sqlite_backend):
        """node_id is stored and retrieved correctly."""
        sqlite_backend.insert_batch([_record(node_id="node-production-3")])

        events, _ = sqlite_backend.query()
        assert events[0]["node_id"] == "node-production-3"

    def test_multiple_node_ids(self, sqlite_backend):
        """Records from different nodes are all stored correctly."""
        sqlite_backend.insert_batch([_record(node_id="node-1")])
        sqlite_backend.insert_batch([_record(node_id="node-2")])

        events, total = sqlite_backend.query()
        assert total == 2
        node_ids = {e["node_id"] for e in events}
        assert node_ids == {"node-1", "node-2"}


# ---------------------------------------------------------------------------
# get_hit_rate_counts — request-denominated hit/request counts (Issue #1257)
# ---------------------------------------------------------------------------


class TestGetHitRateCounts:
    """Regression tests for Issue #1257.

    The dashboard "On-Mode Hit Rate" card was OPERATION-denominated (one
    increment per cache operation, which can be >1 per request on some
    paths e.g. MCP activated-repo search), while search_event_log analytics
    are REQUEST-denominated (exactly one row per user request). This caused
    the two numbers to diverge on any path performing more than one on-mode
    embedding operation per request.

    get_hit_rate_counts(mode) must count REQUESTS (rows), not operations:
    a single row where BOTH providers recorded an "on"-mode hit must count
    as exactly ONE request and ONE hit — never two.
    """

    def test_single_provider_on_mode_hit_counts_as_one_request_one_hit(
        self, sqlite_backend
    ):
        sqlite_backend.insert_batch(
            [_record(voyage_cache_mode="on", voyage_cache_hit=True)]
        )

        counts = sqlite_backend.get_hit_rate_counts("on")
        assert counts == {"hits": 1, "requests": 1}

    def test_single_provider_on_mode_miss_counts_as_one_request_zero_hits(
        self, sqlite_backend
    ):
        sqlite_backend.insert_batch(
            [_record(voyage_cache_mode="on", voyage_cache_hit=False)]
        )

        counts = sqlite_backend.get_hit_rate_counts("on")
        assert counts == {"hits": 0, "requests": 1}

    def test_two_providers_on_mode_same_request_counts_as_one_request_one_hit(
        self, sqlite_backend
    ):
        """The core #1257 reproduction: a single request that performs TWO
        on-mode embedding operations (voyage AND cohere, both hits) must be
        counted as ONE request with ONE hit -- never as two operations.
        This is the exact divergence the issue reports on the MCP
        activated-repo path (>1 on-mode op per request vs 1 log row/request).
        """
        sqlite_backend.insert_batch(
            [
                _record(
                    voyage_cache_mode="on",
                    voyage_cache_hit=True,
                    cohere_cache_mode="on",
                    cohere_cache_hit=True,
                )
            ]
        )

        counts = sqlite_backend.get_hit_rate_counts("on")
        assert counts == {"hits": 1, "requests": 1}, (
            "A single request with 2 on-mode operations must count as 1 "
            f"request / 1 hit, not inflate the operation count. Got: {counts}"
        )

    def test_mixed_modes_only_on_mode_rows_counted(self, sqlite_backend):
        """Rows in shadow mode must not pollute the "on" mode counts, and
        vice versa."""
        sqlite_backend.insert_batch(
            [
                _record(voyage_cache_mode="on", voyage_cache_hit=True),
                _record(voyage_cache_mode="on", voyage_cache_hit=False),
                _record(voyage_cache_mode="shadow", voyage_cache_hit=True),
            ]
        )

        counts = sqlite_backend.get_hit_rate_counts("on")
        assert counts == {"hits": 1, "requests": 2}

        shadow_counts = sqlite_backend.get_hit_rate_counts("shadow")
        assert shadow_counts == {"hits": 1, "requests": 1}

    def test_one_provider_on_other_provider_shadow_still_counts_request(
        self, sqlite_backend
    ):
        """A request where voyage is 'on' (hit) but cohere is 'shadow' must
        still count as one on-mode request with one on-mode hit — the
        cohere column must not suppress or double the count."""
        sqlite_backend.insert_batch(
            [
                _record(
                    voyage_cache_mode="on",
                    voyage_cache_hit=True,
                    cohere_cache_mode="shadow",
                    cohere_cache_hit=True,
                )
            ]
        )

        counts = sqlite_backend.get_hit_rate_counts("on")
        assert counts == {"hits": 1, "requests": 1}

    def test_no_rows_returns_zero_zero(self, sqlite_backend):
        counts = sqlite_backend.get_hit_rate_counts("on")
        assert counts == {"hits": 0, "requests": 0}


# ---------------------------------------------------------------------------
# PostgreSQL backend tests (skipped when PG unavailable)
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_sel_backend() -> "Iterator[tuple]":
    """Yield (backend, unique_username) backed by a live PG pool.

    Pool is always closed in finally even if the preflight check fails.
    Teardown deletes test rows by username inside the try block before pool.close().
    """
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.services.search_event_log_writer import (
        SearchEventLogPostgresBackend,
    )

    pool = ConnectionPool(_TEST_DSN)
    unique_user = f"test-sel-{uuid.uuid4().hex[:8]}"
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        backend = SearchEventLogPostgresBackend(pool)
        yield backend, unique_user
        # Teardown: delete test rows created during the test.
        with pool.connection() as conn:
            conn.execute(
                "DELETE FROM search_event_log WHERE username = %s", (unique_user,)
            )
            conn.commit()
    finally:
        pool.close()


@pytest.mark.skipif(
    not _PG_AVAILABLE,
    reason="psycopg not installed or TEST_POSTGRES_DSN not set",
)
class TestSearchEventLogPostgresBackend:
    """Round-trip tests for SearchEventLogPostgresBackend using a live PostgreSQL DB."""

    def test_insert_and_query_single_record(self, pg_sel_backend):
        """insert_batch([r]) followed by query() returns the record."""
        backend, username = pg_sel_backend
        r = _record(username=username, query_text="pg round-trip test")
        backend.insert_batch([r])

        events, total = backend.query(username=username)
        assert total >= 1
        matching = [e for e in events if e["query_text"] == "pg round-trip test"]
        assert len(matching) == 1
        assert matching[0]["username"] == username

    def test_insert_batch_empty_is_noop(self, pg_sel_backend):
        """insert_batch([]) does not raise and stores nothing."""
        backend, username = pg_sel_backend
        before_total = backend.query(username=username)[1]
        backend.insert_batch([])
        after_total = backend.query(username=username)[1]
        assert after_total == before_total

    def test_nullable_fields_round_trip(self, pg_sel_backend):
        """NULL optional fields round-trip correctly through PG."""
        backend, username = pg_sel_backend
        r = _record(
            username=username,
            repo_alias=None,
            voyage_cache_hit=None,
            voyage_cache_mode=None,
            voyage_latency_ms=None,
            cohere_cache_hit=None,
            cohere_cache_mode=None,
            cohere_latency_ms=None,
            correlation_id=None,
        )
        backend.insert_batch([r])

        events, _ = backend.query(username=username)
        assert events, "Expected at least one event"
        e = events[0]
        assert e["repo_alias"] is None
        assert e["voyage_cache_hit"] is None
        assert e["voyage_cache_mode"] is None
        assert e["voyage_latency_ms"] is None
        assert e["cohere_cache_hit"] is None
        assert e["cohere_cache_mode"] is None
        assert e["cohere_latency_ms"] is None
        assert e["correlation_id"] is None

    def test_prune_older_than_removes_old_records(self, pg_sel_backend):
        """prune_older_than() deletes records older than the cutoff."""
        backend, username = pg_sel_backend
        old_ts = time.time() - 1000
        new_ts = time.time()

        backend.insert_batch([_record(username=username, timestamp=old_ts)])
        backend.insert_batch([_record(username=username, timestamp=new_ts)])

        cutoff = time.time() - 500
        backend.prune_older_than(cutoff)

        events, total = backend.query(username=username)
        assert total == 1
        assert abs(events[0]["timestamp"] - new_ts) < 1.0

    def test_filter_by_search_type(self, pg_sel_backend):
        """query(search_type=...) filters correctly in PG."""
        backend, username = pg_sel_backend
        backend.insert_batch([_record(username=username, search_type="semantic")])
        backend.insert_batch([_record(username=username, search_type="fts")])

        events, total = backend.query(username=username, search_type="semantic")
        assert total == 1
        assert events[0]["search_type"] == "semantic"

    def test_insert_multiple_records(self, pg_sel_backend):
        """Multiple inserts accumulate correctly."""
        backend, username = pg_sel_backend
        for i in range(5):
            backend.insert_batch([_record(username=username, query_text=f"pg-q{i}")])

        _, total = backend.query(username=username)
        assert total == 5

    def test_get_hit_rate_counts_two_providers_one_request(self, pg_sel_backend):
        """Issue #1257: a single request with 2 on-mode operations (voyage +
        cohere, both hits) must count as 1 request / 1 hit in PostgreSQL too.

        Uses a unique username to isolate this test's rows from any other
        concurrent data in the shared table (get_hit_rate_counts aggregates
        the whole table, so we can only assert deltas here by using a
        temporary marker: a mode string namespaced with the unique username
        substring is NOT possible since mode is a real enum value, so instead
        we assert on a known BASELINE-plus-delta by reading counts before and
        after the insert).
        """
        backend, username = pg_sel_backend
        before = backend.get_hit_rate_counts("on")
        backend.insert_batch(
            [
                _record(
                    username=username,
                    voyage_cache_mode="on",
                    voyage_cache_hit=True,
                    cohere_cache_mode="on",
                    cohere_cache_hit=True,
                )
            ]
        )
        after = backend.get_hit_rate_counts("on")

        assert after["requests"] == before["requests"] + 1, (
            "A single request with 2 on-mode operations must add exactly 1 "
            f"to the request count. before={before}, after={after}"
        )
        assert after["hits"] == before["hits"] + 1, (
            "A single request with 2 on-mode hits must add exactly 1 to the "
            f"hit count, not 2. before={before}, after={after}"
        )

    def test_get_hit_rate_counts_miss_does_not_increment_hits(self, pg_sel_backend):
        """A recorded on-mode MISS increments requests but not hits."""
        backend, username = pg_sel_backend
        before = backend.get_hit_rate_counts("on")
        backend.insert_batch(
            [_record(username=username, voyage_cache_mode="on", voyage_cache_hit=False)]
        )
        after = backend.get_hit_rate_counts("on")

        assert after["requests"] == before["requests"] + 1
        assert after["hits"] == before["hits"]
