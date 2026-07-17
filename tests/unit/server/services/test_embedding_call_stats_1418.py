"""Unit tests for EmbeddingCallRecord + dual-backend embedding_call_stats
storage (Story #1418 Phase 1 of 3 -- foundation only, see issue #1418 for
full scope).

SQLite tests use a real temp-file database (Anti-Mock -- no mocking of the
database layer). PostgreSQL tests use a live database from TEST_POSTGRES_DSN
and are skipped when psycopg is absent or the env var is unset -- mirrors
tests/unit/server/storage/test_search_embed_event_backends_1293.py's
established pattern for Story #1293.
"""

import os
import time
import uuid
from typing import Iterator

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
    from code_indexer.server.services.embedding_call_stats import (
        EmbeddingCallStatsSqliteBackend,
    )

    db_path = str(tmp_path / "test_embedding_call_stats.db")
    return EmbeddingCallStatsSqliteBackend(db_path)


def _record(**overrides):
    from code_indexer.server.services.embedding_call_stats import EmbeddingCallRecord

    defaults = dict(
        provider="voyageai",
        call_type="embed",
        model="voyage-code-3",
        item_count=10,
        token_count=500,
        batch_size=10,
        purpose="query",
        success=True,
        latency_ms=120,
        occurred_at=time.time(),
    )
    defaults.update(overrides)
    return EmbeddingCallRecord(**defaults)


# ---------------------------------------------------------------------------
# EmbeddingCallRecord shape / validation
# ---------------------------------------------------------------------------


class TestEmbeddingCallRecordConstruction:
    def test_valid_record_constructs(self):
        r = _record()
        assert r.provider == "voyageai"
        assert r.call_type == "embed"
        assert r.model == "voyage-code-3"
        assert r.item_count == 10
        assert r.token_count == 500
        assert r.batch_size == 10
        assert r.purpose == "query"
        assert r.success is True
        assert r.latency_ms == 120


class TestEmbeddingCallRecordVocabularyRejection:
    def test_invalid_provider_raises(self):
        with pytest.raises(ValueError):
            _record(provider="openai")

    def test_invalid_call_type_raises(self):
        with pytest.raises(ValueError):
            _record(call_type="translate")

    def test_invalid_purpose_raises(self):
        with pytest.raises(ValueError):
            _record(purpose="unknown_purpose")


class TestEmbeddingCallRecordFieldRejection:
    def test_empty_model_raises(self):
        with pytest.raises(ValueError):
            _record(model="")

    @pytest.mark.parametrize(
        "field", ["item_count", "token_count", "batch_size", "latency_ms"]
    )
    def test_negative_numeric_field_raises(self, field):
        with pytest.raises(ValueError):
            _record(**{field: -1})

    def test_non_bool_success_raises(self):
        with pytest.raises(ValueError):
            _record(success="yes")


class TestEmbeddingCallRecordDefaults:
    def test_nullable_context_fields_default_none(self):
        r = _record()
        assert r.golden_repo_alias is None
        assert r.job_id is None
        assert r.node_id is None


class TestEmbeddingCallRecordVocabularyAcceptance:
    @pytest.mark.parametrize("provider", ["voyageai", "cohere"])
    def test_all_valid_providers_accepted(self, provider):
        assert _record(provider=provider).provider == provider

    @pytest.mark.parametrize("call_type", ["embed", "embed_multimodal", "rerank"])
    def test_all_valid_call_types_accepted(self, call_type):
        assert _record(call_type=call_type).call_type == call_type

    @pytest.mark.parametrize(
        "purpose",
        ["index", "refresh", "query", "temporal", "key_test", "cache_shadow_audit"],
    )
    def test_all_valid_purposes_accepted(self, purpose):
        assert _record(purpose=purpose).purpose == purpose


# ---------------------------------------------------------------------------
# EmbeddingCallStatsSqliteBackend
# ---------------------------------------------------------------------------


class TestEmbeddingCallStatsSqliteBackendInsert:
    def test_insert_single_record(self, sqlite_backend):
        sqlite_backend.insert_batch([_record()])
        results = sqlite_backend.query()
        assert len(results) == 1
        assert results[0].provider == "voyageai"

    def test_insert_batch_empty_is_noop(self, sqlite_backend):
        sqlite_backend.insert_batch([])
        assert sqlite_backend.query() == []

    def test_insert_multiple_records_in_one_batch(self, sqlite_backend):
        records = [_record(job_id=f"job-{i}") for i in range(5)]
        sqlite_backend.insert_batch(records)
        results = sqlite_backend.query()
        assert len(results) == 5


class TestEmbeddingCallStatsSqliteBackendQueryFilters:
    def test_query_filters_by_provider(self, sqlite_backend):
        sqlite_backend.insert_batch(
            [_record(provider="voyageai"), _record(provider="cohere")]
        )
        results = sqlite_backend.query(provider="cohere")
        assert len(results) == 1
        assert results[0].provider == "cohere"

    def test_query_filters_by_purpose_and_golden_repo_alias(self, sqlite_backend):
        sqlite_backend.insert_batch(
            [
                _record(purpose="query", golden_repo_alias="repoA"),
                _record(purpose="index", golden_repo_alias="repoB"),
            ]
        )
        by_purpose = sqlite_backend.query(purpose="index")
        assert len(by_purpose) == 1
        assert by_purpose[0].purpose == "index"

        by_alias = sqlite_backend.query(golden_repo_alias="repoA")
        assert len(by_alias) == 1
        assert by_alias[0].golden_repo_alias == "repoA"

    def test_query_filters_by_job_id_and_time_range(self, sqlite_backend):
        now = time.time()
        sqlite_backend.insert_batch(
            [
                _record(job_id="job-1", occurred_at=now - 100),
                _record(job_id="job-2", occurred_at=now),
            ]
        )
        by_job = sqlite_backend.query(job_id="job-2")
        assert len(by_job) == 1
        assert by_job[0].job_id == "job-2"

        by_time = sqlite_backend.query(start_time=now - 1, end_time=now + 1)
        assert len(by_time) == 1
        assert by_time[0].job_id == "job-2"


class TestEmbeddingCallStatsSqliteBackendQueryOrdering:
    def test_query_orders_by_occurred_at_desc(self, sqlite_backend):
        now = time.time()
        sqlite_backend.insert_batch(
            [
                _record(occurred_at=now - 10, job_id="old"),
                _record(occurred_at=now, job_id="new"),
            ]
        )
        results = sqlite_backend.query()
        assert results[0].job_id == "new"
        assert results[1].job_id == "old"

    def test_query_respects_limit(self, sqlite_backend):
        sqlite_backend.insert_batch([_record() for _ in range(10)])
        results = sqlite_backend.query(limit=3)
        assert len(results) == 3


class TestEmbeddingCallStatsSqliteBackendDelete:
    def test_delete_where_removes_old_records(self, sqlite_backend):
        now = time.time()
        sqlite_backend.insert_batch(
            [_record(occurred_at=now - 1000), _record(occurred_at=now)]
        )
        deleted_count = sqlite_backend.delete_where(occurred_at_before=now - 500)
        assert deleted_count == 1
        assert len(sqlite_backend.query()) == 1

    def test_delete_where_no_matches_returns_zero(self, sqlite_backend):
        sqlite_backend.insert_batch([_record(occurred_at=time.time())])
        deleted_count = sqlite_backend.delete_where(occurred_at_before=0)
        assert deleted_count == 0


class TestEmbeddingCallStatsSqliteBackendRoundtrip:
    def test_nullable_fields_roundtrip_as_none(self, sqlite_backend):
        sqlite_backend.insert_batch([_record()])
        results = sqlite_backend.query()
        assert results[0].golden_repo_alias is None
        assert results[0].job_id is None
        assert results[0].node_id is None

    def test_success_false_roundtrips_correctly(self, sqlite_backend):
        sqlite_backend.insert_batch([_record(success=False)])
        results = sqlite_backend.query()
        assert results[0].success is False


class TestEmbeddingCallStatsSqliteBackendSchemaFault:
    def test_schema_setup_failure_logs_warning_and_does_not_raise(
        self, tmp_path, caplog
    ):
        """A directory path cannot be opened as a SQLite database file --
        real filesystem fault, no mocking."""
        import logging

        from code_indexer.server.services.embedding_call_stats import (
            EmbeddingCallStatsSqliteBackend,
        )

        with caplog.at_level(logging.WARNING):
            EmbeddingCallStatsSqliteBackend(str(tmp_path))  # must not raise

        assert any("schema setup failed" in record.message for record in caplog.records)


class TestEmbeddingCallStatsSqliteBackendOperationFaults:
    def test_insert_batch_reraises_on_corrupted_database(self, sqlite_backend):
        with open(sqlite_backend._db_path, "wb") as f:
            f.write(b"not a valid sqlite file")
        with pytest.raises(Exception):
            sqlite_backend.insert_batch([_record()])

    def test_query_returns_empty_list_on_corrupted_database(self, sqlite_backend):
        with open(sqlite_backend._db_path, "wb") as f:
            f.write(b"not a valid sqlite file")
        assert sqlite_backend.query() == []

    def test_delete_where_returns_zero_on_corrupted_database(self, sqlite_backend):
        with open(sqlite_backend._db_path, "wb") as f:
            f.write(b"not a valid sqlite file")
        assert sqlite_backend.delete_where(occurred_at_before=time.time()) == 0


# ---------------------------------------------------------------------------
# EmbeddingCallStatsPostgresBackend (skipped when PG unavailable)
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_backend() -> "Iterator[tuple]":
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.services.embedding_call_stats import (
        EmbeddingCallStatsPostgresBackend,
    )

    pool = ConnectionPool(_TEST_DSN)
    unique_job_prefix = f"test-ecs-{uuid.uuid4().hex[:8]}"
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        backend = EmbeddingCallStatsPostgresBackend(pool)
        yield backend, unique_job_prefix
        with pool.connection() as conn:
            conn.execute(
                "DELETE FROM embedding_call_stats WHERE job_id LIKE %s",
                (f"{unique_job_prefix}%",),
            )
            conn.commit()
    finally:
        pool.close()


@pytest.mark.skipif(
    not _PG_AVAILABLE, reason="psycopg not installed or TEST_POSTGRES_DSN not set"
)
class TestEmbeddingCallStatsPostgresBackend:
    def test_insert_and_query_single_record(self, pg_backend):
        backend, prefix = pg_backend
        backend.insert_batch([_record(job_id=prefix)])
        results = backend.query(job_id=prefix)
        assert len(results) == 1
        assert results[0].provider == "voyageai"

    def test_insert_batch_multi_row_single_transaction(self, pg_backend):
        backend, prefix = pg_backend
        records = [_record(job_id=f"{prefix}-{i}") for i in range(10)]
        backend.insert_batch(records)
        results = backend.query(job_id=f"{prefix}-0")
        assert len(results) == 1

    def test_query_filters_by_provider(self, pg_backend):
        backend, prefix = pg_backend
        backend.insert_batch(
            [
                _record(job_id=prefix, provider="voyageai"),
                _record(job_id=prefix, provider="cohere"),
            ]
        )
        results = backend.query(job_id=prefix, provider="cohere")
        assert len(results) == 1
        assert results[0].provider == "cohere"
