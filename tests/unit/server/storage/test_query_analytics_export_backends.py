"""Tests for QueryAnalyticsExportSqliteBackend (Story #1160).

More thorough backend-level coverage per spec, plus tests for
SearchEventLogSqliteBackend.query_for_export().
"""

import time
import uuid

import pytest

from code_indexer.server.services.query_analytics_export_service import (
    QueryAnalyticsExportSqliteBackend,
)
from code_indexer.server.services.search_event_log_writer import (
    SearchEventLogSqliteBackend,
    SearchEventRecord,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def export_backend(tmp_db):
    return QueryAnalyticsExportSqliteBackend(tmp_db)


@pytest.fixture
def search_backend(tmp_db):
    return SearchEventLogSqliteBackend(tmp_db)


def _make_export_record(export_id=None, status="pending", **kwargs):
    return {
        "id": export_id or str(uuid.uuid4()),
        "initiated_by": kwargs.get("initiated_by", "alice"),
        "created_at": kwargs.get("created_at", time.time()),
        "status": status,
        "filter_summary": kwargs.get("filter_summary", "All searches"),
        "file_path": kwargs.get("file_path", None),
        "file_size_bytes": kwargs.get("file_size_bytes", None),
        "row_count": kwargs.get("row_count", None),
        "error_message": kwargs.get("error_message", None),
        "retention_until": kwargs.get("retention_until", None),
    }


def _make_search_event(**kwargs):
    return SearchEventRecord(
        timestamp=kwargs.get("timestamp", time.time()),
        username=kwargs.get("username", "testuser"),
        repo_alias=kwargs.get("repo_alias", "my-repo"),
        search_type=kwargs.get("search_type", "semantic"),
        query_text=kwargs.get("query_text", "hello world"),
        voyage_cache_hit=kwargs.get("voyage_cache_hit", None),
        voyage_cache_mode=kwargs.get("voyage_cache_mode", None),
        voyage_latency_ms=kwargs.get("voyage_latency_ms", None),
        cohere_cache_hit=kwargs.get("cohere_cache_hit", None),
        cohere_cache_mode=kwargs.get("cohere_cache_mode", None),
        cohere_latency_ms=kwargs.get("cohere_latency_ms", None),
        total_latency_ms=kwargs.get("total_latency_ms", 100),
        result_count=kwargs.get("result_count", 5),
        node_id=kwargs.get("node_id", "node-1"),
        correlation_id=kwargs.get("correlation_id", None),
    )


# ---------------------------------------------------------------------------
# QueryAnalyticsExportSqliteBackend — thorough coverage
# ---------------------------------------------------------------------------


class TestExportBackendCreateAndList:
    def test_create_and_retrieve(self, export_backend):
        rec = _make_export_record(export_id="aaa-001", initiated_by="alice")
        export_backend.create_export(rec)
        results = export_backend.list_exports()
        assert len(results) == 1
        assert results[0]["id"] == "aaa-001"
        assert results[0]["initiated_by"] == "alice"
        assert results[0]["status"] == "pending"
        assert results[0]["file_path"] is None

    def test_list_multiple_ordered_by_created_at_desc(self, export_backend):
        now = time.time()
        rec1 = _make_export_record(export_id="id-1", created_at=now - 100)
        rec2 = _make_export_record(export_id="id-2", created_at=now - 50)
        rec3 = _make_export_record(export_id="id-3", created_at=now)
        export_backend.create_export(rec1)
        export_backend.create_export(rec2)
        export_backend.create_export(rec3)
        results = export_backend.list_exports()
        assert len(results) == 3
        # Should be descending by created_at
        assert results[0]["id"] == "id-3"
        assert results[1]["id"] == "id-2"
        assert results[2]["id"] == "id-1"

    def test_list_by_id_found(self, export_backend):
        rec = _make_export_record(export_id="target-id")
        export_backend.create_export(rec)
        export_backend.create_export(_make_export_record(export_id="other-id"))
        results = export_backend.list_exports(export_id="target-id")
        assert len(results) == 1
        assert results[0]["id"] == "target-id"

    def test_list_by_id_not_found_returns_empty(self, export_backend):
        results = export_backend.list_exports(export_id="does-not-exist")
        assert results == []

    def test_list_empty_db_returns_empty(self, export_backend):
        assert export_backend.list_exports() == []


class TestExportBackendUpdate:
    def test_update_status(self, export_backend):
        rec = _make_export_record(export_id="u-1")
        export_backend.create_export(rec)
        export_backend.update_export("u-1", status="running")
        result = export_backend.list_exports(export_id="u-1")[0]
        assert result["status"] == "running"

    def test_update_multiple_fields(self, export_backend):
        rec = _make_export_record(export_id="u-2")
        export_backend.create_export(rec)
        export_backend.update_export(
            "u-2",
            status="completed",
            file_path="/tmp/u-2.xlsx",
            file_size_bytes=1024,
            row_count=42,
            retention_until=time.time() + 86400,
        )
        result = export_backend.list_exports(export_id="u-2")[0]
        assert result["status"] == "completed"
        assert result["file_path"] == "/tmp/u-2.xlsx"
        assert result["file_size_bytes"] == 1024
        assert result["row_count"] == 42
        assert result["retention_until"] is not None

    def test_update_error_message(self, export_backend):
        rec = _make_export_record(export_id="u-3")
        export_backend.create_export(rec)
        export_backend.update_export("u-3", status="failed", error_message="oops")
        result = export_backend.list_exports(export_id="u-3")[0]
        assert result["status"] == "failed"
        assert result["error_message"] == "oops"

    def test_update_no_fields_is_noop(self, export_backend):
        rec = _make_export_record(export_id="u-4", status="pending")
        export_backend.create_export(rec)
        export_backend.update_export("u-4")  # no kwargs
        result = export_backend.list_exports(export_id="u-4")[0]
        assert result["status"] == "pending"  # unchanged

    def test_update_unknown_field_raises(self, export_backend):
        rec = _make_export_record(export_id="u-5")
        export_backend.create_export(rec)
        with pytest.raises(ValueError, match="unknown field"):
            export_backend.update_export("u-5", hacked_field="malicious")


class TestExportBackendEviction:
    def test_evict_expired_row_and_file(self, export_backend, tmp_path):
        xlsx = tmp_path / "exp.xlsx"
        xlsx.write_bytes(b"fake")
        now = time.time()
        rec = _make_export_record(
            export_id="e-1",
            status="completed",
            file_path=str(xlsx),
            retention_until=now - 100,
        )
        export_backend.create_export(rec)
        count = export_backend.evict_old_exports(now_ts=now)
        assert count == 1
        assert not xlsx.exists()
        assert export_backend.list_exports() == []

    def test_evict_preserves_unexpired(self, export_backend):
        now = time.time()
        rec = _make_export_record(export_id="keep", retention_until=now + 86400)
        export_backend.create_export(rec)
        count = export_backend.evict_old_exports(now_ts=now)
        assert count == 0
        assert len(export_backend.list_exports()) == 1

    def test_evict_null_retention_not_evicted(self, export_backend):
        rec = _make_export_record(export_id="null-ret", retention_until=None)
        export_backend.create_export(rec)
        count = export_backend.evict_old_exports(now_ts=time.time() + 1e9)
        assert count == 0
        assert len(export_backend.list_exports()) == 1

    def test_evict_missing_file_no_error(self, export_backend):
        now = time.time()
        rec = _make_export_record(
            export_id="missing",
            file_path="/does/not/exist.xlsx",
            retention_until=now - 1,
        )
        export_backend.create_export(rec)
        count = export_backend.evict_old_exports(now_ts=now)
        assert count == 1  # row deleted even if file missing

    def test_evict_mixed_expired_and_unexpired(self, export_backend, tmp_path):
        now = time.time()
        old_file = tmp_path / "old.xlsx"
        old_file.write_bytes(b"old")
        old = _make_export_record(
            export_id="old",
            file_path=str(old_file),
            retention_until=now - 1,
        )
        new = _make_export_record(export_id="new", retention_until=now + 86400)
        export_backend.create_export(old)
        export_backend.create_export(new)
        count = export_backend.evict_old_exports(now_ts=now)
        assert count == 1
        remaining = export_backend.list_exports()
        assert len(remaining) == 1
        assert remaining[0]["id"] == "new"


# ---------------------------------------------------------------------------
# SearchEventLogSqliteBackend.query_for_export()
# ---------------------------------------------------------------------------


class TestQueryForExport:
    def test_returns_all_rows_no_filter(self, search_backend):
        search_backend.insert_batch([_make_search_event(), _make_search_event()])
        rows = search_backend.query_for_export({})
        assert len(rows) == 2

    def test_ordered_asc_by_timestamp(self, search_backend):
        now = time.time()
        search_backend.insert_batch(
            [
                _make_search_event(timestamp=now - 100),
                _make_search_event(timestamp=now - 200),
                _make_search_event(timestamp=now),
            ]
        )
        rows = search_backend.query_for_export({})
        ts_vals = [r["timestamp"] for r in rows]
        assert ts_vals == sorted(ts_vals)

    def test_filter_by_user(self, search_backend):
        search_backend.insert_batch(
            [
                _make_search_event(username="alice"),
                _make_search_event(username="bob"),
            ]
        )
        rows = search_backend.query_for_export({"user": "alice"})
        assert all(r["username"] == "alice" for r in rows)
        assert len(rows) == 1

    def test_filter_by_repo_alias(self, search_backend):
        search_backend.insert_batch(
            [
                _make_search_event(repo_alias="repo-a"),
                _make_search_event(repo_alias="repo-b"),
            ]
        )
        rows = search_backend.query_for_export({"repo_alias": "repo-a"})
        assert len(rows) == 1
        assert rows[0]["repo_alias"] == "repo-a"

    def test_filter_by_search_type(self, search_backend):
        search_backend.insert_batch(
            [
                _make_search_event(search_type="semantic"),
                _make_search_event(search_type="fts"),
            ]
        )
        rows = search_backend.query_for_export({"search_type": "semantic"})
        assert len(rows) == 1
        assert rows[0]["search_type"] == "semantic"

    def test_filter_search_type_all_skipped(self, search_backend):
        search_backend.insert_batch(
            [
                _make_search_event(search_type="semantic"),
                _make_search_event(search_type="fts"),
            ]
        )
        rows = search_backend.query_for_export({"search_type": "all"})
        assert len(rows) == 2

    def test_filter_from_timestamp(self, search_backend):
        now = time.time()
        search_backend.insert_batch(
            [
                _make_search_event(timestamp=now - 200),
                _make_search_event(timestamp=now - 50),
            ]
        )
        rows = search_backend.query_for_export({"from_timestamp": now - 100})
        assert len(rows) == 1

    def test_filter_to_timestamp(self, search_backend):
        now = time.time()
        search_backend.insert_batch(
            [
                _make_search_event(timestamp=now - 200),
                _make_search_event(timestamp=now - 50),
            ]
        )
        rows = search_backend.query_for_export({"to_timestamp": now - 100})
        assert len(rows) == 1

    def test_cache_hit_filter_hits_only(self, search_backend):
        search_backend.insert_batch(
            [
                _make_search_event(voyage_cache_hit=True),
                _make_search_event(voyage_cache_hit=False),
                _make_search_event(cohere_cache_hit=True),
            ]
        )
        rows = search_backend.query_for_export({"cache_hit_filter": "hits_only"})
        assert len(rows) == 2
        for r in rows:
            assert r.get("voyage_cache_hit") or r.get("cohere_cache_hit")

    def test_cache_hit_filter_misses_only(self, search_backend):
        search_backend.insert_batch(
            [
                _make_search_event(voyage_cache_hit=True),
                _make_search_event(voyage_cache_hit=False, cohere_cache_hit=False),
                _make_search_event(voyage_cache_hit=None, cohere_cache_hit=None),
            ]
        )
        rows = search_backend.query_for_export({"cache_hit_filter": "misses_only"})
        assert len(rows) == 2

    def test_cache_hit_filter_all_returns_all(self, search_backend):
        search_backend.insert_batch(
            [
                _make_search_event(voyage_cache_hit=True),
                _make_search_event(voyage_cache_hit=False),
            ]
        )
        rows = search_backend.query_for_export({"cache_hit_filter": "all"})
        assert len(rows) == 2

    def test_empty_db_returns_empty_list(self, search_backend):
        rows = search_backend.query_for_export({})
        assert rows == []
