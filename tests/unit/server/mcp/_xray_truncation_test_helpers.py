"""Shared, non-test fixtures/helpers for the Bug #1928 xray_truncation
test suite -- factored out so the suite can be split across several
small, focused test files without duplicating fixture code in each one.

Not itself a test module: pytest only collects files matching test_*.py,
so this file is imported (never collected) by its sibling test files.
"""

from __future__ import annotations

import json as json_module
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.mcp.handlers import xray_truncation as xt

logger = logging.getLogger(__name__)

# Provable upper bound on pagination loops below (Rule 14,
# anti-unbounded-loop) -- every fixture in this suite uses well under 500
# entries.
MAX_PAGES_SAFETY = 200


def _safe_close(closeable: Any, label: str) -> None:
    """Best-effort close that never raises -- a cleanup-time failure must
    never mask the original exception it's cleaning up after, nor prevent
    sibling resources from also being released."""
    try:
        closeable.close()
    except Exception:  # noqa: BLE001 -- cleanup must never raise
        logger.warning("%s: close() failed", label, exc_info=True)


@pytest.fixture
def cache_factory(tmp_path: Path):
    """Factory fixture: build a REAL PayloadCache (on-disk SQLite) with the
    given max_fetch_size_chars, guaranteeing close() teardown for every
    successfully-created instance -- one failing close() must not prevent
    the others from being released."""
    created: List[PayloadCache] = []

    def _make(max_fetch_size_chars: int) -> PayloadCache:
        config = PayloadCacheConfig(
            preview_size_chars=max_fetch_size_chars,
            max_fetch_size_chars=max_fetch_size_chars,
            cache_ttl_seconds=900,
            cleanup_interval_seconds=60,
        )
        db_path = tmp_path / f"payload_cache_{len(created)}.db"
        cache = PayloadCache(db_path=db_path, config=config)
        try:
            cache.initialize()
        except Exception:
            _safe_close(cache, "cache_factory setup")
            raise
        created.append(cache)
        return cache

    yield _make

    for created_cache in created:
        _safe_close(created_cache, "cache_factory teardown")


def make_match(i: int) -> Dict[str, Any]:
    return {
        "file_path": f"file_{i}.py",
        "line_number": i,
        "code_snippet": f"snippet body {i}",
        "language": "python",
        "evaluator_decision": True,
    }


def make_tiny_match(i: int) -> Dict[str, Any]:
    """A deliberately minimal match shape (~25 chars serialized) for tests
    that need MANY entries to still fit comfortably under a real,
    un-overridden default budget (payload_max_fetch_size_chars=5000)."""
    return {"file_path": f"f{i}.py", "line": i}


def make_error(i: int) -> Dict[str, Any]:
    return {
        "file_path": f"err_{i}.py",
        "line_number": None,
        "error_type": "AttributeError",
        "error_message": f"node has no attribute 'x{i}'",
    }


def make_finding(i: int) -> Dict[str, Any]:
    return {
        "pattern": f"pattern_{i}",
        "message": f"finding message body {i}",
        "involved": [i, i + 1],
        "signatures": [f"fn sig_{i}(x: i32) -> i32"],
    }


def fetch_all_pages(
    payload_cache: PayloadCache, cache_handle: str
) -> List[Dict[str, Any]]:
    """Walk every page via xt.fetch_cached_page, asserting each page's
    `content` parses standalone. Returns the parsed page dicts in order."""
    pages: List[Dict[str, Any]] = []
    for page_num in range(1, MAX_PAGES_SAFETY + 1):
        result = xt.fetch_cached_page(payload_cache, cache_handle, page_num)
        parsed = json_module.loads(result["content"])
        pages.append(parsed)
        if not result["has_more"]:
            return pages
    raise AssertionError(
        f"pagination did not terminate within {MAX_PAGES_SAFETY} pages"
    )


# ---------------------------------------------------------------------------
# Live PostgreSQL gating (mirrors test_per_consumer_rate_limiter_live_pg_1332.py)
# ---------------------------------------------------------------------------

HAS_PSYCOPG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG = True
except ImportError:
    pass


@pytest.fixture(scope="module")
def pg_dsn():
    if not HAS_PSYCOPG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        import psycopg

        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    return dsn


@pytest.fixture
def pg_cache(pg_dsn: str):
    import psycopg

    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.payload_cache_backend import (
        PayloadCachePostgresBackend,
    )

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS payload_cache")

    pool = ConnectionPool(pg_dsn, min_size=1, max_size=2)
    cache: Optional[PayloadCache] = None
    try:
        backend = PayloadCachePostgresBackend(pool)
        config = PayloadCacheConfig(preview_size_chars=300, max_fetch_size_chars=300)
        cache = PayloadCache(
            db_path=Path("/unused"), config=config, storage_backend=backend
        )
        cache.initialize()
    except Exception:
        if cache is not None:
            _safe_close(cache, "pg_cache setup")
        _safe_close(pool, "pg_cache setup (pool)")
        raise

    try:
        yield cache
    finally:
        _safe_close(cache, "pg_cache teardown")
        _safe_close(pool, "pg_cache teardown (pool)")
        try:
            with psycopg.connect(pg_dsn, autocommit=True) as conn:
                conn.execute("DROP TABLE IF EXISTS payload_cache")
        except Exception:  # noqa: BLE001
            logger.warning("pg_cache teardown: DROP TABLE failed", exc_info=True)
