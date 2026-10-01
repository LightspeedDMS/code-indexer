"""RED-first tests for Bug #1553: LogsBackend.query_logs() capability parity.

LogAggregatorService.query()/query_all() support a `levels` LIST filter and a
free-text `search` filter; LogsSqliteBackend.query_logs() supported neither
before this fix (single `level` string only, no text search, no explicit
sort_order). Routing cluster reads through query_logs() unchanged would
silently drop those capabilities.

Exercises the SQLite backend directly (always runs -- SQLite is real,
in-process, no external dependency, no mocks). The PostgreSQL-specific
counterpart lives in test_logs_backend_query_extension_postgres_1553.py.

Also proves the new `is_cross_node_backend` capability flag exists and is
False for SQLite (same-process/same-file store) -- the explicit,
non-string-matching capability check that replaces the fragile
`"Postgres" in type(x).__name__` pattern.
"""

from __future__ import annotations

import pytest


def _seed_three_levels(backend, *, prefix: str = "") -> None:
    """Seed one ERROR, one WARNING, one INFO row with distinct messages."""
    backend.insert_log(
        timestamp="2026-01-01T00:00:00Z",
        level="ERROR",
        source="mod.a",
        message=f"{prefix}disk failure detected",
    )
    backend.insert_log(
        timestamp="2026-01-01T00:00:01Z",
        level="WARNING",
        source="mod.b",
        message=f"{prefix}cache miss for needle-token",
    )
    backend.insert_log(
        timestamp="2026-01-01T00:00:02Z",
        level="INFO",
        source="mod.c",
        message=f"{prefix}startup complete",
    )


@pytest.fixture
def seeded_backend(tmp_path):
    """A real LogsSqliteBackend seeded with one ERROR/WARNING/INFO row each."""
    from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

    backend = LogsSqliteBackend(db_path=str(tmp_path / "logs.db"))
    _seed_three_levels(backend)
    return backend


class TestIsCrossNodeBackendCapabilityFlag:
    def test_sqlite_backend_is_not_cross_node(self, tmp_path):
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        backend = LogsSqliteBackend(db_path=str(tmp_path / "logs.db"))
        assert backend.is_cross_node_backend is False

    def test_protocol_declares_is_cross_node_backend_attribute(self):
        from code_indexer.server.storage.protocols import LogsBackend

        # An annotation-only Protocol attribute (no assigned value) shows up
        # in __annotations__, not in dir() -- dir() only lists actual bound
        # attributes/methods.
        assert "is_cross_node_backend" in LogsBackend.__annotations__

    def test_protocol_declares_query_logs_levels_search_sort_order(self):
        import inspect

        from code_indexer.server.storage.protocols import LogsBackend

        signature = inspect.signature(LogsBackend.query_logs)
        assert "levels" in signature.parameters
        assert "search" in signature.parameters
        assert "sort_order" in signature.parameters


class TestSqliteBackendLevelsAndSearch:
    def test_levels_list_filters_multiple_levels(self, seeded_backend):
        results, total = seeded_backend.query_logs(
            levels=["ERROR", "WARNING"], limit=100
        )

        assert total == 2
        levels_seen = {r["level"] for r in results}
        assert levels_seen == {"ERROR", "WARNING"}

    def test_search_matches_message_text_case_insensitive(self, seeded_backend):
        results, total = seeded_backend.query_logs(search="NEEDLE-TOKEN", limit=100)

        assert total == 1
        assert "needle-token" in results[0]["message"]

    def test_levels_takes_precedence_over_single_level(self, seeded_backend):
        # level="INFO" alone would match 1 row; levels takes precedence.
        results, total = seeded_backend.query_logs(
            level="INFO", levels=["ERROR", "WARNING"], limit=100
        )
        assert total == 2


class TestSqliteBackendInsertLogBatchReturnValue:
    def test_insert_log_batch_returns_true_on_success(self, tmp_path):
        """Bug #1553: insert_log_batch must return a real success signal
        (True), not implicitly None, so SQLiteLogHandler's writer loop can
        distinguish success from failure without relying on exceptions
        alone (the sibling LogsPostgresBackend swallows and never raises).
        """
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        backend = LogsSqliteBackend(db_path=str(tmp_path / "logs.db"))
        # 12-tuple: (timestamp, level, source, message, correlation_id,
        # user_id, request_path, extra_data, node_id, alias, trace_id,
        # span_id) -- Story #1676 AC2 appended trace_id/span_id.
        result = backend.insert_log_batch(
            [
                (
                    "2026-01-01T00:00:00Z",
                    "INFO",
                    "mod.a",
                    "hello",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            ]
        )
        assert result is True


class TestSqliteBackendSortOrder:
    def test_sort_order_asc_returns_oldest_first(self, seeded_backend):
        results, _ = seeded_backend.query_logs(sort_order="asc", limit=100)
        assert results[0]["message"] == "disk failure detected"

    def test_sort_order_desc_is_default_and_returns_newest_first(self, seeded_backend):
        results, _ = seeded_backend.query_logs(limit=100)
        assert results[0]["message"] == "startup complete"

    def test_existing_callers_without_new_params_are_unaffected(self, seeded_backend):
        """Additive-only: pre-existing kwarg-only call shape still works."""
        results, total = seeded_backend.query_logs(level="ERROR", limit=100, offset=0)
        assert total == 1
        assert results[0]["level"] == "ERROR"
