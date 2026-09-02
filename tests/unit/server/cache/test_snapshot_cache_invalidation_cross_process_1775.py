"""GitHub Bug #1775 round 5: ``invalidate_snapshot_caches()`` must ALSO
publish the stale prefix to the cross-process registry (not just the
LOCAL ``ChunkStoreThreadCache``), so a real alias-swap on THIS worker
becomes visible to every OTHER worker/node. See ``chunk_store_cache_
cross_process.py``'s module docstring for the full design rationale.
"""

from pathlib import Path

import pytest

from code_indexer.server.cache import reset_global_cache
from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.storage.shared.chunk_store_cache import (
    reset_global_chunk_store_cache,
)
from code_indexer.storage.shared.chunk_store_cache_cross_process import (
    read_stale_prefixes_registry,
    register_payload_cache,
    reset_registered_payload_cache,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR = [0.1, 0.2, 0.3, 0.4]
CHUNKS_DB_FILENAME = "chunks.db"
PROVIDER_DIR = "voyage-code-3"
INDEX_SUBPATH = Path(".code-indexer") / "index" / PROVIDER_DIR
FAST_TTL_SECONDS = 900
FAST_CLEANUP_INTERVAL_SECONDS = 60


def _make_versioned_snapshot(base: Path, repo: str, version: str, point_id: str):
    snapshot_dir = base / ".versioned" / repo / version
    collection_dir = snapshot_dir / INDEX_SUBPATH
    collection_dir.mkdir(parents=True, exist_ok=True)
    db_path = collection_dir / CHUNKS_DB_FILENAME
    store = ChunkStore(db_path)
    try:
        store.write_batch(
            [{"id": point_id, "vector": VECTOR, "payload": {"path": f"{point_id}.py"}}]
        )
    finally:
        store.close()
    return str(db_path), str(collection_dir), str(snapshot_dir)


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_global_cache()
    reset_global_chunk_store_cache()
    reset_registered_payload_cache()
    yield
    reset_global_chunk_store_cache()
    reset_global_cache()
    reset_registered_payload_cache()


class TestPublishesToRegistryWhenPayloadCacheIsRegistered:
    def test_invalidate_snapshot_caches_publishes_old_target_to_registry(
        self, tmp_path
    ):
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )
        from code_indexer.server.storage.shared.snapshot_paths import (
            is_versioned_snapshot,
        )

        payload_db_path = tmp_path / "payload_cache.db"
        payload_cache = PayloadCache(
            db_path=payload_db_path,
            config=PayloadCacheConfig(
                cache_ttl_seconds=FAST_TTL_SECONDS,
                cleanup_interval_seconds=FAST_CLEANUP_INTERVAL_SECONDS,
            ),
        )
        try:
            payload_cache.initialize()
            register_payload_cache(payload_cache)

            _old_db, _old_coll, old_dir = _make_versioned_snapshot(
                tmp_path, "repo", "v_1", "p1"
            )

            invalidate_snapshot_caches(
                old_dir,
                log_context="[test]",
                is_versioned_snapshot_check=is_versioned_snapshot,
            )

            registry = read_stale_prefixes_registry(payload_cache)
            assert old_dir in registry, (
                "invalidate_snapshot_caches() must publish old_target to "
                "the cross-process registry (via publish_stale_prefix()) "
                "so OTHER worker processes can discover and apply it -- "
                "not just invalidate this process's own local cache."
            )
        finally:
            payload_cache.close()


class TestDoesNotLogPublishedOnFailure:
    """Round-6 code review finding (Codex): the caller ignored publish_
    stale_prefix()'s outcome entirely and always logged "Published..."
    -- a genuine write failure was silently reported as success. Uses
    caplog to inspect REAL log records (no mocking).
    """

    def test_invalidate_snapshot_caches_does_not_log_published_on_publish_failure(
        self, tmp_path, caplog
    ):
        import logging

        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )
        from code_indexer.server.storage.shared.snapshot_paths import (
            is_versioned_snapshot,
        )

        payload_cache = PayloadCache(
            db_path=tmp_path / "payload_cache.db",
            config=PayloadCacheConfig(
                cache_ttl_seconds=FAST_TTL_SECONDS,
                cleanup_interval_seconds=FAST_CLEANUP_INTERVAL_SECONDS,
            ),
        )
        payload_cache.initialize()
        # Force a genuine write failure -- no mocking, a real broken
        # PayloadCache (closed connection manager).
        payload_cache.close()
        payload_cache._conn_manager = None
        register_payload_cache(payload_cache)

        _old_db, _old_coll, old_dir = _make_versioned_snapshot(
            tmp_path, "repo", "v_1", "p1"
        )

        with caplog.at_level(logging.INFO):
            invalidate_snapshot_caches(
                old_dir,
                log_context="[test]",
                is_versioned_snapshot_check=is_versioned_snapshot,
            )

        published_messages = [
            record.message
            for record in caplog.records
            if "Published chunk store cache stale prefix" in record.message
        ]
        assert published_messages == [], (
            "invalidate_snapshot_caches() must NOT log the 'Published...' "
            "success message when the underlying publish genuinely "
            f"failed -- found: {published_messages}"
        )


class TestGracefullyDegradesWithoutRegisteredPayloadCache:
    def test_invalidate_snapshot_caches_does_not_raise_without_payload_cache(
        self, tmp_path
    ):
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )
        from code_indexer.server.storage.shared.snapshot_paths import (
            is_versioned_snapshot,
        )

        # No register_payload_cache() call -- simulates CLI/solo-import
        # contexts or a server that hasn't reached that startup step yet.
        _old_db, _old_coll, old_dir = _make_versioned_snapshot(
            tmp_path, "repo", "v_1", "p1"
        )

        # Must not raise.
        invalidate_snapshot_caches(
            old_dir,
            log_context="[test]",
            is_versioned_snapshot_check=is_versioned_snapshot,
        )


class _SilentlyFailingStoreCache:
    """Mirrors the real PostgreSQL-backend failure shape: a write call
    that completes without raising but does NOT persist --
    retrieve()/has_key() still delegate to the REAL underlying store.
    No SQLite backend can genuinely produce this exact behavior, so a
    test double for this ONE dependency method is architecturally
    necessary to reproduce it (not a mock of the system under test --
    invalidate_snapshot_caches(), publish_stale_prefix(), and
    read_stale_prefixes_registry() all execute for real against it).
    """

    def __init__(self, wrapped_cache):
        self._wrapped_cache = wrapped_cache

    def store_with_key(self, key, content):
        return None  # Silently does NOT persist.

    def retrieve(self, handle, page=0):
        return self._wrapped_cache.retrieve(handle, page=page)

    def has_key(self, key):
        return self._wrapped_cache.has_key(key)


def _make_silently_failing_payload_cache(tmp_path):
    """Returns (wrapped_double, real_cache) -- real_cache must be
    closed by the caller.
    """
    real_cache = PayloadCache(
        db_path=tmp_path / "payload_cache.db",
        config=PayloadCacheConfig(
            cache_ttl_seconds=FAST_TTL_SECONDS,
            cleanup_interval_seconds=FAST_CLEANUP_INTERVAL_SECONDS,
        ),
    )
    real_cache.initialize()
    return _SilentlyFailingStoreCache(real_cache), real_cache


def _assert_publish_failure_warning_logged(caplog) -> None:
    import logging

    publish_warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "publish" in r.message.lower()
    ]
    assert publish_warning_records, (
        "A genuine publish verification failure (write silently did "
        "not persist) must log a WARNING mentioning 'publish' -- got "
        "zero matching log output, reproducing the exact silent-"
        "failure gap this test guards against. All WARNING records: "
        f"{[r.message for r in caplog.records if r.levelno == 30]}"
    )


class TestLogsWarningWhenPublishVerificationFails:
    """Round-8 code review (Claude): publish_stale_prefix() correctly
    returns False when a write silently does not persist (round-7 fix),
    but the caller (_publish_chunk_store_stale_prefix_cross_process)
    only had ``if published: log info`` -- no ``else`` branch -- so a
    False return produced ZERO log output at any level. This is the
    exact PostgreSQL-backend failure shape on the WRITE path this time
    (round 7 fixed the READ path's equivalent gap).
    """

    def test_invalidate_snapshot_caches_logs_a_warning_when_publish_fails(
        self, tmp_path, caplog
    ):
        import logging

        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )
        from code_indexer.server.storage.shared.snapshot_paths import (
            is_versioned_snapshot,
        )
        from code_indexer.storage.shared.chunk_store_cache_cross_process import (
            reset_registry_publish_failure_state,
        )

        reset_registry_publish_failure_state()
        wrapped, real_cache = _make_silently_failing_payload_cache(tmp_path)
        try:
            register_payload_cache(wrapped)  # type: ignore[arg-type]  # test double implements only the methods exercised here
            _old_db, _old_coll, old_dir = _make_versioned_snapshot(
                tmp_path, "repo", "v_1", "p1"
            )
            with caplog.at_level(logging.DEBUG):
                invalidate_snapshot_caches(
                    old_dir,
                    log_context="[test]",
                    is_versioned_snapshot_check=is_versioned_snapshot,
                )
            _assert_publish_failure_warning_logged(caplog)
        finally:
            reset_registered_payload_cache()
            real_cache.close()
