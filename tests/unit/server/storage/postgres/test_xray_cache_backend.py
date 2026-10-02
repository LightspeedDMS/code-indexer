"""Tests for XrayCachePostgresBackend — Epic #1019 / cluster xray evaluator cache.

Uses a MagicMock connection pool (no real PostgreSQL required).
"""

from __future__ import annotations

import operator as _operator
import re as _re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest


def _make_mock_pool(fetchone_return=None, rowcount: int = 0) -> MagicMock:
    """Return a MagicMock mimicking a psycopg ConnectionPool context-manager."""
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    cursor.fetchall.return_value = []
    cursor.rowcount = rowcount
    conn.execute.return_value = cursor
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


def _get_conn(pool: MagicMock) -> Any:
    # Returning Any: chained MagicMock attribute accesses (.return_value.__enter__
    # .return_value) produce Any under mypy — no more precise type is expressible.
    return pool.connection.return_value.__enter__.return_value


@pytest.fixture()
def pool_and_backend():
    """Backend configured for a cache miss (fetchone returns None)."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    pool = _make_mock_pool(fetchone_return=None)
    backend = XrayCachePostgresBackend(pool)
    _get_conn(pool).reset_mock()  # clear schema-setup calls
    return pool, backend


@pytest.fixture()
def store_backend():
    """Backend and connection mock reset for testing store() and cleanup."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    pool = _make_mock_pool(rowcount=0)
    backend = XrayCachePostgresBackend(pool)
    conn = _get_conn(pool)
    conn.reset_mock()
    return backend, conn


@pytest.fixture()
def pool_and_fresh_backend():
    """Backend configured for a cache hit with fake .so bytes."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    fake_bytes = b"\x7fELF fake so content"
    pool = _make_mock_pool(fetchone_return=(fake_bytes,))
    backend = XrayCachePostgresBackend(pool)
    return pool, backend, fake_bytes


# ---------------------------------------------------------------------------
# Tests: schema, miss, hit
# ---------------------------------------------------------------------------


def test_schema_created_on_init() -> None:
    """XrayCachePostgresBackend.__init__ must call CREATE TABLE IF NOT EXISTS."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    pool = _make_mock_pool()
    XrayCachePostgresBackend(pool)
    conn = _get_conn(pool)

    assert conn.execute.called
    assert conn.commit.called
    combined = " ".join(str(c) for c in conn.execute.call_args_list)
    assert "xray_evaluator_cache" in combined
    assert "IF NOT EXISTS" in combined or "if not exists" in combined.lower()


def test_fetch_returns_none_on_miss(pool_and_backend) -> None:
    """fetch() must return None when the query returns no row."""
    _, backend = pool_and_backend
    assert backend.fetch("abc123", "rustc 1.91.0") is None


def test_fetch_returns_bytes_on_fresh_hit(pool_and_fresh_backend) -> None:
    """fetch() must return the so_bytes from the row on a fresh hit."""
    _, backend, fake_bytes = pool_and_fresh_backend
    result = backend.fetch("abc123", "rustc 1.91.0")
    assert result == fake_bytes


def test_fetch_sql_filters_by_compiled_at_and_rustc_version(pool_and_backend) -> None:
    """fetch() SQL must filter by compiled_at AND rustc_version in WHERE clause."""
    pool, backend = pool_and_backend

    backend.fetch("deadbeef", "rustc 1.88.0")

    conn = _get_conn(pool)
    fetch_call = conn.execute.call_args
    sql = str(fetch_call[0][0])
    params = fetch_call[0][1]

    assert "compiled_at" in sql
    assert "rustc_version" in sql
    assert "rustc 1.88.0" in params
    assert len(params) == 3, (
        "fetch() must pass exactly 3 params (hash, version, cutoff)"
    )


def test_fetch_returns_none_on_rustc_version_mismatch(pool_and_backend) -> None:
    """fetch() returns None when rustc_version does not match (SQL returns no row)."""
    pool, backend = pool_and_backend

    result = backend.fetch("abc123", "rustc 1.70.0-mismatched")

    assert result is None
    conn = _get_conn(pool)
    params = conn.execute.call_args[0][1]
    assert "rustc 1.70.0-mismatched" in params, (
        "The mismatched rustc_version must be passed as a query param"
    )


def test_fetch_passes_a_datetime_cutoff_as_its_third_param(pool_and_backend) -> None:
    """fetch() must bind a datetime cutoff as the 3rd query parameter.

    Renamed from test_fetch_returns_none_on_stale_entry, which claimed to
    prove TTL enforcement and did not: this fixture's mock returns
    fetchone() == None unconditionally, so "the entry is stale" is never
    represented anywhere and the None result is the mock's, not the
    cutoff's. A sibling review confirmed it survived flipping `>` to `<`,
    computing the cutoff as now + ttl, and ignoring ttl_seconds entirely.

    What it does legitimately check is the call SHAPE -- a bound datetime
    parameter rather than an interpolated string. The behaviour it used to
    claim is now pinned by test_fetch_serves_a_row_inside_the_ttl,
    test_fetch_does_not_serve_a_row_outside_the_ttl and
    test_fetch_honours_ttl_seconds_rather_than_a_fixed_horizon below, which
    run against FakeCacheTable and evaluate the predicate for real.
    """
    pool, backend = pool_and_backend

    result = backend.fetch("stale_hash", "rustc 1.91.0")

    assert result is None
    conn = _get_conn(pool)
    fetch_call = conn.execute.call_args
    sql = str(fetch_call[0][0])
    params = fetch_call[0][1]

    assert "compiled_at" in sql, "SQL must include compiled_at filter"
    assert len(params) == 3
    cutoff = params[2]
    assert isinstance(cutoff, datetime), (
        f"3rd param must be a datetime cutoff, got {type(cutoff)}"
    )


def test_store_issues_upsert_and_commits(store_backend) -> None:
    """store() must execute INSERT ... ON CONFLICT DO UPDATE and commit."""
    backend, conn = store_backend

    backend.store("cafebabe", "rustc 1.91.0", b"\x7fELF store test", compile_ms=350)

    assert conn.execute.called
    assert conn.commit.called
    all_sql = " ".join(str(c) for c in conn.execute.call_args_list)
    assert "INSERT" in all_sql.upper()
    assert "ON CONFLICT" in all_sql.upper()


def test_store_triggers_cleanup_delete_after_upsert(store_backend) -> None:
    """store() must execute DELETE AFTER the UPSERT (verified by call order)."""
    backend, conn = store_backend

    backend.store("hash99", "rustc 1.91.0", b"so_data", compile_ms=100)

    sqls = [str(c) for c in conn.execute.call_args_list]
    assert len(sqls) >= 2, (
        f"store() must execute at least 2 statements, got {len(sqls)}"
    )
    insert_pos = next((i for i, s in enumerate(sqls) if "INSERT" in s.upper()), None)
    delete_pos = next((i for i, s in enumerate(sqls) if "DELETE" in s.upper()), None)
    assert insert_pos is not None, "store() must execute INSERT"
    assert delete_pos is not None, "store() must execute DELETE cleanup"
    assert insert_pos < delete_pos, "DELETE must come AFTER INSERT"


def test_cleanup_expired_issues_delete_and_returns_count() -> None:
    """_cleanup_expired() must DELETE expired rows and return the count deleted.

    Uses rowcount=3 pool (distinct from store_backend's rowcount=0).
    """
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    pool = _make_mock_pool(rowcount=3)
    backend = XrayCachePostgresBackend(pool)
    conn = _get_conn(pool)
    conn.reset_mock()

    deleted = backend._cleanup_expired()

    assert conn.execute.called
    assert conn.commit.called
    assert deleted == 3
    all_sql = " ".join(str(c) for c in conn.execute.call_args_list)
    assert "DELETE" in all_sql.upper()
    assert "xray_evaluator_cache" in all_sql


def test_fetch_swallows_pg_exception_returns_none() -> None:
    """fetch() must catch all exceptions and return None (never raise)."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    pool = _make_mock_pool()
    conn = _get_conn(pool)
    conn.execute.side_effect = RuntimeError("PG connection lost")
    backend = XrayCachePostgresBackend.__new__(XrayCachePostgresBackend)
    backend._pool = pool

    result = backend.fetch("any", "any")
    assert result is None


def test_store_swallows_pg_exception_does_not_raise() -> None:
    """store() must catch all exceptions and return without raising."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    pool = _make_mock_pool()
    conn = _get_conn(pool)
    conn.execute.side_effect = RuntimeError("PG write failed")
    backend = XrayCachePostgresBackend.__new__(XrayCachePostgresBackend)
    backend._pool = pool

    backend.store("any", "any", b"bytes", compile_ms=0)  # must not raise


def test_ttl_outlives_a_single_query_burst() -> None:
    """The TTL must be long enough for the cache to be worth having.

    This replaces an assertion that pinned the constant to 300. Pinning the
    number tested that nobody edited the line, not that the cache works: at 300s
    any evaluator not re-run within five minutes was recompiled from scratch on
    the next query, on every node, which is the cost this table exists to
    remove. The TTL cannot protect correctness -- source_hash is the composite
    identity (source + ABI + rustc_version) and fetch() re-filters
    rustc_version, so a key can only ever map to one correct .so and age cannot
    make a row wrong -- so what is worth asserting is that it bounds capacity
    without defeating reuse.
    """
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XRAY_CACHE_TTL_SECONDS,
    )

    assert XRAY_CACHE_TTL_SECONDS >= 24 * 60 * 60, (
        "a sub-day TTL recompiles evaluators that are merely infrequent, which "
        "is the case the cluster cache is for"
    )
    assert XRAY_CACHE_TTL_SECONDS <= 30 * 24 * 60 * 60, (
        "the TTL is the only thing reclaiming rows made unreachable by a "
        "toolchain or ABI bump (_cleanup_expired is lazy, on store() only)"
    )


# ---------------------------------------------------------------------------
# Behavioural fake PostgreSQL.
#
# Why this exists: every test above asserts on SQL *text* and parameter
# *types*, never on which rows a given WHERE clause actually selects. A
# sibling review proved that is vacuous -- all four of these mutations left
# the suite green:
#
#   1. fetch():            `AND compiled_at > %s`  ->  `< %s`
#                          (serves ONLY expired rows)
#   2. fetch():            cutoff = now + ttl      (nothing is ever served)
#   3. _cleanup_expired(): `WHERE compiled_at <= %s` -> `>= %s`
#                          (deletes the FRESH rows, keeps the expired ones)
#   4. fetch():            ignores ttl_seconds, hardcodes a 10-year cutoff
#
# FakeCacheTable is NOT a general SQL engine. It recognises exactly the
# statement shapes XrayCachePostgresBackend emits and applies their real
# semantics: the comparison operator actually written in the WHERE clause,
# the real cutoff datetime actually passed as a bound parameter, and the
# real ORDER BY / OFFSET / LIMIT window. That is what makes the tests below
# behavioural -- each of the four mutations changes which rows come back, so
# each one turns an assertion red.
#
# Any statement it does not recognise raises, so a future SQL change can
# never silently fall through to a vacuous pass.
# ---------------------------------------------------------------------------

_COMPILED_AT_PREDICATE = _re.compile(r"compiled_at\s*(<=|>=|<|>)\s*%s")
_ORDER_BY_COMPILED_AT = _re.compile(r"ORDER\s+BY\s+compiled_at\s+(ASC|DESC)", _re.I)
_OPS = {
    ">": _operator.gt,
    ">=": _operator.ge,
    "<": _operator.lt,
    "<=": _operator.le,
}


class _FakeCursor:
    """psycopg-shaped result: .fetchone(), .fetchall(), .rowcount."""

    def __init__(self, rows, rowcount: int) -> None:
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeCacheTable:
    """In-memory xray_evaluator_cache that EVALUATES the emitted SQL."""

    def __init__(self) -> None:
        self.rows: dict = {}
        self.now = datetime.now(timezone.utc)  # what NOW() means in an INSERT
        self.statements: list = []
        self.commits = 0

    # -- seeding ---------------------------------------------------------

    def seed(
        self,
        source_hash: str,
        rustc_version: str,
        so_bytes: bytes,
        compiled_at,
        compile_ms: int = 0,
    ) -> None:
        self.rows[source_hash] = {
            "rustc_version": rustc_version,
            "so_bytes": so_bytes,
            "compiled_at": compiled_at,
            "compile_ms": compile_ms,
        }

    # -- execution -------------------------------------------------------

    def execute(self, sql: str, params=()) -> _FakeCursor:
        self.statements.append((sql, params))
        normalized = " ".join(str(sql).split())
        upper = normalized.upper()
        if upper.startswith("CREATE TABLE"):
            return _FakeCursor([], 0)
        if upper.startswith("SELECT SO_BYTES"):
            return self._select(normalized, params)
        if upper.startswith("INSERT"):
            return self._upsert(normalized, params)
        if upper.startswith("DELETE"):
            return self._delete(normalized, upper, params)
        raise AssertionError(f"FakeCacheTable: unrecognised SQL: {normalized}")

    # -- statement handlers ----------------------------------------------

    def _select(self, sql: str, params) -> _FakeCursor:
        match = _COMPILED_AT_PREDICATE.search(sql)
        assert match, (
            f"fetch() must constrain compiled_at against a bound parameter; got: {sql}"
        )
        assert "source_hash = %s" in sql and "rustc_version = %s" in sql, sql
        op = _OPS[match.group(1)]
        source_hash, rustc_version, cutoff = params
        row = self.rows.get(source_hash)
        if row is None or row["rustc_version"] != rustc_version:
            return _FakeCursor([], 0)
        if not op(row["compiled_at"], cutoff):
            return _FakeCursor([], 0)
        return _FakeCursor([(row["so_bytes"],)], 1)

    def _upsert(self, sql: str, params) -> _FakeCursor:
        assert "ON CONFLICT" in sql.upper(), sql
        source_hash, rustc_version, so_bytes, compile_ms = params
        self.seed(source_hash, rustc_version, so_bytes, self.now, compile_ms)
        return _FakeCursor([], 1)

    def _delete(self, sql: str, upper: str, params) -> _FakeCursor:
        assert "SOURCE_HASH IN (" in upper, (
            "a chunked DELETE must select its victims through a bounded "
            f"subquery; got: {sql}"
        )
        assert "LIMIT %s" in sql, (
            "every DELETE must be chunked with a bound LIMIT so one store() "
            f"can never stall on a huge delete; got: {sql}"
        )
        ordered = self._ordered(sql)
        if "OFFSET %s" in sql:
            keep_rows, limit = params
            victims = ordered[keep_rows : keep_rows + limit]
        else:
            match = _COMPILED_AT_PREDICATE.search(sql)
            assert match, f"TTL DELETE must compare compiled_at: {sql}"
            op = _OPS[match.group(1)]
            cutoff, limit = params
            victims = [h for h in ordered if op(self.rows[h]["compiled_at"], cutoff)][
                :limit
            ]
        for source_hash in victims:
            del self.rows[source_hash]
        return _FakeCursor([], len(victims))

    def _ordered(self, sql: str) -> list:
        match = _ORDER_BY_COMPILED_AT.search(sql)
        assert match, (
            "a chunked DELETE must ORDER BY compiled_at so the chunk it takes "
            f"is the intended end of the table, not an arbitrary one; got: {sql}"
        )
        reverse = match.group(1).upper() == "DESC"
        return [
            source_hash
            for source_hash, _row in sorted(
                self.rows.items(),
                key=lambda kv: (kv[1]["compiled_at"], kv[0]),
                reverse=reverse,
            )
        ]


class _FakeConn:
    def __init__(self, table: FakeCacheTable) -> None:
        self._table = table

    def execute(self, sql, params=()):
        return self._table.execute(sql, params)

    def commit(self) -> None:
        self._table.commits += 1


def _fake_pool(table: FakeCacheTable) -> MagicMock:
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=_FakeConn(table))
    ctx.__exit__ = MagicMock(return_value=False)
    pool.connection.return_value = ctx
    return pool


def _backend_on(table: FakeCacheTable, **kwargs):
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )

    return XrayCachePostgresBackend(_fake_pool(table), **kwargs)


RUSTC = "rustc 1.91.0"


# ---------------------------------------------------------------------------
# TTL enforcement -- behaviour, in both directions.
# ---------------------------------------------------------------------------


def test_fetch_serves_a_row_inside_the_ttl() -> None:
    """A row compiled 30s ago must be SERVED under a 60s TTL.

    Kills mutation 1 (`>` -> `<`: the fresh row stops matching) and
    mutation 2 (cutoff = now + ttl: nothing ever matches).
    """
    table = FakeCacheTable()
    now = datetime.now(timezone.utc)
    table.seed("fresh", RUSTC, b"\x7fELF fresh", now - timedelta(seconds=30))

    backend = _backend_on(table)

    assert backend.fetch("fresh", RUSTC, ttl_seconds=60) == b"\x7fELF fresh"


def test_fetch_does_not_serve_a_row_outside_the_ttl() -> None:
    """A row compiled 120s ago must NOT be served under a 60s TTL.

    Kills mutation 1 (`>` -> `<`: the expired row becomes the only match)
    and mutation 4 (hardcoded 10-year cutoff: the expired row matches).
    """
    table = FakeCacheTable()
    now = datetime.now(timezone.utc)
    table.seed("expired", RUSTC, b"\x7fELF expired", now - timedelta(seconds=120))

    backend = _backend_on(table)

    assert backend.fetch("expired", RUSTC, ttl_seconds=60) is None


def test_fetch_honours_ttl_seconds_rather_than_a_fixed_horizon() -> None:
    """The SAME row must be served or withheld depending only on ttl_seconds.

    One row, one hour old, two TTLs either side of its age. Kills mutation 4
    (a hardcoded horizon makes both calls answer the same way) and mutation 2.
    """
    table = FakeCacheTable()
    now = datetime.now(timezone.utc)
    table.seed("hourly", RUSTC, b"\x7fELF hourly", now - timedelta(hours=1))

    backend = _backend_on(table)

    assert backend.fetch("hourly", RUSTC, ttl_seconds=2 * 3600) == b"\x7fELF hourly"
    assert backend.fetch("hourly", RUSTC, ttl_seconds=60) is None


def test_cleanup_expired_deletes_the_expired_row_and_keeps_the_fresh_one() -> None:
    """_cleanup_expired() must remove rows OLDER than the cutoff, only.

    Kills mutation 3 (`<=` -> `>=`: the fresh row is deleted and the expired
    one survives, which is exactly backwards).
    """
    table = FakeCacheTable()
    now = datetime.now(timezone.utc)
    table.seed("expired", RUSTC, b"old", now - timedelta(seconds=120))
    table.seed("fresh", RUSTC, b"new", now - timedelta(seconds=10))

    backend = _backend_on(table)
    deleted = backend._cleanup_expired(ttl_seconds=60)

    assert deleted == 1
    assert set(table.rows) == {"fresh"}, (
        "the TTL delete removed the wrong side of the cutoff"
    )


# ---------------------------------------------------------------------------
# Capacity bound.
#
# The TTL alone does not bound this table. At 7 days a busy cluster can keep
# far more rows alive than the 10 GiB cidx-postgres PVC holds (1 replica, no
# autoscaling), and a full Postgres refuses writes for sessions, jobs, leases
# and LEADER ELECTION -- a whole-cluster outage two hops from a cache that
# swallows its own exceptions. So the table must be bounded by ROW COUNT too,
# and the eviction must be chunked: at the measured 2.0s per 500 rows an
# unchunked generation delete is ~20 seconds inside a user query.
# ---------------------------------------------------------------------------


def _seed_aged_rows(table: FakeCacheTable, count: int, *, age_base_seconds: int = 0):
    """Seed `count` rows with strictly increasing age (row 0 is newest)."""
    now = datetime.now(timezone.utc)
    for i in range(count):
        table.seed(
            f"row{i:03d}",
            RUSTC,
            f"so-{i}".encode(),
            now - timedelta(seconds=age_base_seconds + i + 1),
        )
    return now


def test_evict_over_capacity_keeps_exactly_the_n_most_recent_rows() -> None:
    """Rows beyond the N most recent by compiled_at must be deleted."""
    table = FakeCacheTable()
    _seed_aged_rows(table, 5)
    backend = _backend_on(table)

    deleted = backend._evict_over_capacity(max_rows=3, chunk_rows=10)

    assert deleted == 2
    assert set(table.rows) == {"row000", "row001", "row002"}, (
        "eviction must drop the OLDEST rows, keeping the N most recent"
    )


def test_evict_over_capacity_is_a_no_op_when_under_the_bound() -> None:
    """A table already under the row cap must issue no deletions."""
    table = FakeCacheTable()
    _seed_aged_rows(table, 3)
    backend = _backend_on(table)

    deleted = backend._evict_over_capacity(max_rows=10, chunk_rows=10)

    assert deleted == 0
    assert len(table.rows) == 3


def test_evict_over_capacity_deletes_at_most_one_chunk_per_call() -> None:
    """A single call must never delete more than chunk_rows rows.

    This is the anti-stall guarantee: a 2,000-row overflow must not become a
    single multi-second DELETE inside a user's xray query.
    """
    table = FakeCacheTable()
    _seed_aged_rows(table, 20)
    backend = _backend_on(table)

    deleted = backend._evict_over_capacity(max_rows=0, chunk_rows=5)

    assert deleted == 5, "capacity eviction must be capped at chunk_rows per call"
    assert len(table.rows) == 15


def test_cleanup_expired_deletes_at_most_one_chunk_per_call() -> None:
    """The TTL delete must be chunked for the same anti-stall reason."""
    table = FakeCacheTable()
    _seed_aged_rows(table, 20, age_base_seconds=3600)
    backend = _backend_on(table)

    deleted = backend._cleanup_expired(ttl_seconds=60, chunk_rows=5)

    assert deleted == 5, "TTL cleanup must be capped at chunk_rows per call"
    assert len(table.rows) == 15


def test_cleanup_expired_takes_the_oldest_rows_first() -> None:
    """A partial TTL chunk must reclaim the oldest rows, not an arbitrary set."""
    table = FakeCacheTable()
    _seed_aged_rows(table, 6, age_base_seconds=3600)
    backend = _backend_on(table)

    backend._cleanup_expired(ttl_seconds=60, chunk_rows=2)

    assert "row005" not in table.rows and "row004" not in table.rows, (
        "the oldest two rows must be the ones reclaimed first"
    )
    assert len(table.rows) == 4


def test_store_bounds_the_table_without_being_asked() -> None:
    """store() must apply the capacity bound, not merely the TTL.

    Every row here is fresh, so the TTL sweep reclaims nothing; only a row
    cap can keep this table off the PVC ceiling.
    """
    table = FakeCacheTable()
    _seed_aged_rows(table, 5)
    backend = _backend_on(table, max_rows=3, delete_chunk_rows=10)

    backend.store("brand-new", RUSTC, b"\x7fELF new", compile_ms=10)

    assert len(table.rows) == 3, (
        "store() must evict down to the row cap; the TTL alone cannot bound "
        "a table of rows that are all fresh"
    )
    assert "brand-new" in table.rows


def test_store_never_issues_an_unbounded_delete() -> None:
    """Every DELETE store() emits must carry a bound LIMIT."""
    table = FakeCacheTable()
    _seed_aged_rows(table, 5)
    backend = _backend_on(table, max_rows=2, delete_chunk_rows=3)

    backend.store("brand-new", RUSTC, b"\x7fELF new", compile_ms=10)

    deletes = [
        " ".join(str(sql).split())
        for sql, _params in table.statements
        if str(sql).strip().upper().startswith("DELETE")
    ]
    assert deletes, "store() must emit at least one DELETE"
    for sql in deletes:
        assert "LIMIT %s" in sql, f"unbounded DELETE emitted by store(): {sql}"


# ---------------------------------------------------------------------------
# The chosen bounds must actually fit the hardware they are sized for.
# ---------------------------------------------------------------------------

_MIB_PER_ROW = 1.65  # measured: heap + PK + toast index for one 4.28 MiB .so
_SECONDS_PER_ROW_DELETED = 2.0 / 500  # measured: 2.0s per 500-row DELETE
_PVC_MIB = 10 * 1024  # cidx-postgres PVC, 1 replica, no autoscaling


def test_row_cap_leaves_the_coordination_plane_room_on_the_pvc() -> None:
    """XRAY_CACHE_MAX_ROWS must not be able to fill the 10 GiB PVC.

    A full cidx-postgres refuses writes for sessions, jobs, leases and leader
    election, so the cache may claim at most half the volume.
    """
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XRAY_CACHE_MAX_ROWS,
    )

    worst_case_mib = XRAY_CACHE_MAX_ROWS * _MIB_PER_ROW
    assert worst_case_mib <= _PVC_MIB / 2, (
        f"{XRAY_CACHE_MAX_ROWS} rows is {worst_case_mib:.0f} MiB of a "
        f"{_PVC_MIB} MiB PVC -- the coordination plane and WAL need the rest"
    )
    assert XRAY_CACHE_MAX_ROWS >= 2000, (
        "a toolchain or ABI bump makes two generations coexist for the whole "
        "7-day TTL, so the cap must hold two generations of real working set"
    )


def test_delete_chunk_cannot_stall_a_user_query() -> None:
    """One store() must never spend a multi-second stall deleting rows."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XRAY_CACHE_DELETE_CHUNK_ROWS,
    )

    # store() emits at most two chunked DELETEs: the TTL sweep and the
    # capacity eviction.
    worst_case_seconds = 2 * XRAY_CACHE_DELETE_CHUNK_ROWS * _SECONDS_PER_ROW_DELETED
    assert worst_case_seconds < 1.0, (
        f"a {XRAY_CACHE_DELETE_CHUNK_ROWS}-row chunk costs "
        f"{worst_case_seconds:.2f}s per store() at the measured delete rate"
    )


# ---------------------------------------------------------------------------
# Observability. store() still swallows its exceptions (it is a cache and must
# degrade silently) -- but a cluster whose cache has been failing every write
# for a week, or whose table is over capacity and not draining, must be
# visible as something other than a log line nobody greps.
# ---------------------------------------------------------------------------


def test_store_failure_records_a_telemetry_counter_and_does_not_raise() -> None:
    """A swallowed store() failure must still increment a counter."""
    from code_indexer.server.storage.postgres.xray_cache_backend import (
        XrayCachePostgresBackend,
    )
    from tests.unit.server.telemetry.otel_test_support import (
        active_application_metrics_singleton,
        find_metric,
    )

    pool = _make_mock_pool()
    conn = _get_conn(pool)
    conn.execute.side_effect = RuntimeError("PG write failed")
    backend = XrayCachePostgresBackend.__new__(XrayCachePostgresBackend)
    backend._pool = pool

    with active_application_metrics_singleton() as (_metrics, reader):
        backend.store("any", RUSTC, b"bytes", compile_ms=0)  # must not raise

    metric = find_metric(reader, "cidx.xray.cache_store_failures")
    assert metric is not None, (
        "a swallowed store() failure must record cidx.xray.cache_store_failures"
    )
    assert sum(dp.value for dp in metric.data.data_points) == 1


def test_eviction_records_a_telemetry_counter() -> None:
    """Rows reclaimed by the capacity bound must be counted."""
    from tests.unit.server.telemetry.otel_test_support import (
        active_application_metrics_singleton,
        find_metric,
    )

    table = FakeCacheTable()
    _seed_aged_rows(table, 5)
    backend = _backend_on(table, max_rows=2, delete_chunk_rows=10)

    with active_application_metrics_singleton() as (_metrics, reader):
        backend.store("brand-new", RUSTC, b"\x7fELF new", compile_ms=10)

    metric = find_metric(reader, "cidx.xray.cache_rows_evicted")
    assert metric is not None, "eviction must record cidx.xray.cache_rows_evicted"
    by_reason = {
        dp.attributes.get("reason"): dp.value for dp in metric.data.data_points
    }
    assert by_reason.get("capacity") == 4, by_reason


def test_saturated_eviction_chunk_records_a_backlog_counter() -> None:
    """A chunk that came back full means the table is still over the bound.

    That is the alertable condition: the cache is above its capacity budget
    and one store() could not drain it.
    """
    from tests.unit.server.telemetry.otel_test_support import (
        active_application_metrics_singleton,
        find_metric,
    )

    table = FakeCacheTable()
    _seed_aged_rows(table, 30)
    backend = _backend_on(table, max_rows=2, delete_chunk_rows=5)

    with active_application_metrics_singleton() as (_metrics, reader):
        backend.store("brand-new", RUSTC, b"\x7fELF new", compile_ms=10)

    metric = find_metric(reader, "cidx.xray.cache_eviction_backlog")
    assert metric is not None, (
        "a saturated eviction chunk must record cidx.xray.cache_eviction_backlog"
    )
    assert sum(dp.value for dp in metric.data.data_points) >= 1
