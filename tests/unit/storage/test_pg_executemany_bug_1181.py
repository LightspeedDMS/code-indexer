"""Real-PostgreSQL regression tests for the Bug #1181 batch-write methods.

These tests exercise the PG backends against a REAL psycopg3 connection pool
(NOT a mock), because the original Bug #1181 unit tests used a FakeConn mock that
defined ``executemany`` on the connection object -- a method that the real
psycopg v3 ``Connection`` does NOT have (it lives on ``Cursor``). The mock hid a
hard ``AttributeError: 'Connection' object has no attribute 'executemany'`` that
made BOTH batch writers silent no-ops (fail-open) in production PG mode:

  * PayloadCachePostgresBackend.store_batch          (Perf Fix #1)
  * QueryEmbeddingCachePostgresBackend.touch_last_used_batch (Perf Fix #2)

A real-PG test proves the rows are actually persisted / updated.

To run, point CIDX_TEST_PG_DSN at a throwaway PostgreSQL database, e.g.:

    CIDX_TEST_PG_DSN="postgresql://user@127.0.0.1:55433/cidx_bench" \
        PYTHONPATH=src pytest tests/unit/storage/test_pg_executemany_bug_1181.py -v

The tests are SKIPPED when CIDX_TEST_PG_DSN is unset so they never run in
fast-automation.sh (which has no PostgreSQL).
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest

logger = logging.getLogger(__name__)

_DSN = os.environ.get("CIDX_TEST_PG_DSN", "")

pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="CIDX_TEST_PG_DSN not set -- real-PostgreSQL test skipped",
)


@pytest.fixture()
def pool():
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    p = ConnectionPool(_DSN, min_size=1, max_size=4, name="bug1181-test")
    yield p
    # Best-effort teardown: closing the underlying pool can fail if a connection
    # is mid-flight. Log it with context rather than swallowing silently.
    try:
        p._pool.close()  # type: ignore[attr-defined]
    except Exception:
        logger.warning("bug1181-test pool teardown failed", exc_info=True)


def test_payload_store_batch_actually_persists_rows(pool):
    """store_batch MUST persist every entry to a real PG table (Fix #1).

    Before the fix this raised 'Connection' object has no attribute
    'executemany', was swallowed fail-open, and persisted nothing -- so the
    cache_handle returned to the client was a dead handle (retrieve -> 404).
    """
    from code_indexer.server.storage.postgres.payload_cache_backend import (
        PayloadCachePostgresBackend,
    )

    backend = PayloadCachePostgresBackend(pool)

    h1 = str(uuid.uuid4())
    h2 = str(uuid.uuid4())
    big1 = "A" * 5000
    big2 = "B" * 6000
    entries = [
        (h1, big1, big1[:2000], 900),
        (h2, big2, big2[:2000], 900),
    ]

    backend.store_batch(entries)

    row1 = backend.retrieve(h1)
    row2 = backend.retrieve(h2)

    assert row1 is not None, "store_batch did not persist entry 1 (Fix #1 broken)"
    assert row2 is not None, "store_batch did not persist entry 2 (Fix #1 broken)"
    assert row1["content"] == big1
    assert row2["content"] == big2


def test_query_embedding_touch_last_used_batch_updates_rows(pool):
    """touch_last_used_batch MUST update last_used on real PG rows (Fix #2).

    Before the fix this raised the same AttributeError and silently no-op'd, so
    last_used was never refreshed (LRU eviction bookkeeping broken under load).
    """
    from code_indexer.server.storage.postgres.query_embedding_cache_backend import (
        QueryEmbeddingCachePostgresBackend,
    )

    backend = QueryEmbeddingCachePostgresBackend(pool)

    cache_key = f"s:test:{uuid.uuid4()}"
    provider = "voyage"
    model = "voyage-code-3"
    dimension = 1024
    vector = b"\x00\x00\x80?" * dimension  # 4 bytes * dim, arbitrary float32 LE

    # Seed one row via the normal upsert path, then touch it via the batch path.
    backend.upsert(
        cache_key,
        provider,
        model,
        dimension,
        vector,
        created_at=1000.0,
        last_used=1000.0,
    )
    backend.touch_last_used_batch([(cache_key, provider, model, dimension, 2000.0)])

    # Read last_used back directly to confirm the batched UPDATE actually ran.
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT last_used FROM query_embedding_cache
            WHERE cache_key=%s AND provider=%s AND model=%s AND dimension=%s
            """,
            (cache_key, provider, model, dimension),
        ).fetchone()

    assert row is not None, "seed row missing -- upsert failed"
    assert float(row[0]) == 2000.0, (
        f"touch_last_used_batch did not update last_used (Fix #2 broken): got {row[0]}"
    )
