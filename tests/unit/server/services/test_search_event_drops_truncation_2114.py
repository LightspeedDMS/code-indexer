"""Bug #2114: dropped search events are counted; truncated query text is marked.

Truncation: query_text is stored capped at QUERY_TEXT_MAX_CHARS (500); the
row's query_text_truncated column says whether the search's text was longer
(NULL for rows written before the column existed). Every door hands the full
text to the shared recording module, which applies the cap and sets the flag.

Drops: the bounded SearchEventLogWriter counts every event it drops on queue
overflow (per process), and the count is visible through /health (per server
process, with its node id),
the OTEL counter and the Query Analytics Export page.

Rows are read back from a REAL store (SQLite and PostgreSQL) written by the
REAL SearchEventLogWriter.
"""

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List

import pytest

from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
)
from tests.unit.server.services.search_event_recording_harness import (
    Recorder,
    make_user,
    recorder,
)

_KINDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    ),
]

_LONG_QUERY = "token refresh " * 43  # 602 characters


class _SemanticSearch:
    """SemanticQueryManager double: returns two known results."""

    def query_user_repositories(self, **_: Any) -> Dict[str, Any]:
        return {
            "results": [
                {"file_path": f"src/module_{i}.py", "similarity_score": 0.9}
                for i in range(2)
            ],
            "total_results": 2,
            "query_metadata": {},
        }


class _ActivatedRepos:
    def __init__(self, repo: Path) -> None:
        self._repo = repo

    def list_activated_repositories(self, username: str) -> List[Dict[str, Any]]:
        return [{"user_alias": "example-repo", "username": username}]

    def get_activated_repo_path(self, username: str, alias: str) -> str:
        return str(self._repo)

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return True


class Env(SimpleNamespace):
    rec: Recorder
    repo: Path


@pytest.fixture(params=_KINDS)
def env(request, tmp_path) -> Iterator[Env]:
    repo = tmp_path / "repo"
    repo.mkdir()
    with recorder(request.param, tmp_path) as rec:
        yield Env(rec=rec, repo=repo)


def _rest_query(env: Env, query_text: str) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth import dependencies
    from code_indexer.server.routers.inline_query import register_query_routes

    app = FastAPI()
    register_query_routes(
        app,
        semantic_query_manager=_SemanticSearch(),
        activated_repo_manager=_ActivatedRepos(env.repo),
    )
    app.state.search_event_log_writer = env.rec.writer
    app.state.payload_cache = None
    app.state.access_filtering_service = None
    app.dependency_overrides[dependencies.get_current_user] = lambda: make_user("alice")
    return TestClient(app).post(
        "/api/query",
        json={"query_text": query_text, "repository_alias": "example-repo"},
    )


class TestQueryTextTruncationIsMarked:
    def test_long_rest_query_is_stored_capped_and_flagged_truncated(self, env):
        assert len(_LONG_QUERY) > 500

        assert _rest_query(env, _LONG_QUERY).status_code == 200
        assert _rest_query(env, "token refresh").status_code == 200

        long_row, short_row = env.rec.rows()
        assert long_row["query_text"] == _LONG_QUERY[:500]
        assert bool(long_row["query_text_truncated"]) is True
        assert short_row["query_text"] == "token refresh"
        assert short_row["query_text_truncated"] in (False, 0)

    def test_long_mcp_search_code_query_is_flagged_truncated(self, env, tmp_path):
        from unittest.mock import patch

        from code_indexer.server.mcp.handlers._utils import (
            _format_omni_response,
            _mcp_response,
        )
        from code_indexer.server.mcp.handlers.search import search_code

        def _one_repository_searched(params: Any, user: Any) -> Dict[str, Any]:
            formatted = _format_omni_response(
                all_results=[{"file_path": "a.py", "source_repo": "first-repo"}],
                response_format="grouped",
                total_repos_searched=1,
                errors={},
            )
            return _mcp_response({"success": True, "results": formatted})

        app_module = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(search_event_log_writer=env.rec.writer)
            ),
            activated_repo_manager=None,
            golden_repo_manager=None,
        )
        with patch("code_indexer.server.mcp.handlers._utils.app_module", app_module):
            with patch(
                "code_indexer.server.mcp.handlers.search.code_search._omni_search_code",
                _one_repository_searched,
            ):
                search_code(
                    {
                        "repository_alias": ["first-repo"],
                        "query_text": _LONG_QUERY,
                        "search_mode": "fts",
                    },
                    make_user("alice"),
                )

        [row] = env.rec.rows()
        assert row["query_text"] == _LONG_QUERY[:500]
        assert bool(row["query_text_truncated"]) is True

    def test_deferred_temporal_search_keeps_the_truncation_mark(self, env, tmp_path):
        import json

        from code_indexer.server.services.search_event_context import (
            SearchEventContext,
        )
        from code_indexer.server.services.search_event_recording import (
            _deferred_key,
            record_deferred_search,
            remember_deferred_search,
        )
        from tests.unit.server.services.search_event_recording_harness import (
            payload_cache_of,
        )

        with payload_cache_of("sqlite", tmp_path) as cache:
            context = SearchEventContext(
                username="alice",
                repo_alias="example-repo-global",
                search_type="temporal",
                query_text=_LONG_QUERY,
            )
            remember_deferred_search(
                cache,
                {"error_code": "TEMPORAL_QUERY_DEFERRED", "job_id": "job-1"},
                context=context,
                correlation_id=None,
                started_at=time.time(),
            )
            stored = json.loads(cache.retrieve_full(_deferred_key("job-1", "alice")))
            assert len(stored["query_text"]) == 501

            record_deferred_search(
                cache, env.rec.writer, job_id="job-1", username="alice", result_count=2
            )

        [row] = env.rec.rows()
        assert row["search_type"] == "temporal"
        assert row["query_text"] == _LONG_QUERY[:500]
        assert bool(row["query_text_truncated"]) is True


_MIGRATIONS = (
    Path(__file__).parents[4]
    / "src/code_indexer/server/storage/postgres/migrations/sql"
)
_OLD_ROW = (
    "INSERT INTO search_event_log (timestamp, username, repo_alias, search_type, "
    "query_text, total_latency_ms, result_count, node_id) VALUES "
    "(1.0, 'alice', 'example-repo', 'semantic', 'old query', 5, 1, 'node-1')"
)


def _record(query_text: str) -> Any:
    from tests.unit.server.services.search_event_real_stores import make_record

    return make_record(query_text=query_text, query_text_truncated=True)


# The search_event_log DDL DatabaseSchema shipped before Bug #2114: existing
# solo databases still have this table, without query_text_truncated.
_PRE_2114_SQLITE_DDL = """
    CREATE TABLE IF NOT EXISTS search_event_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp           REAL NOT NULL,
        username            TEXT NOT NULL,
        repo_alias          TEXT,
        search_type         TEXT NOT NULL,
        query_text          TEXT NOT NULL,
        voyage_cache_hit    INTEGER,
        voyage_cache_mode   TEXT,
        voyage_latency_ms   INTEGER,
        cohere_cache_hit    INTEGER,
        cohere_cache_mode   TEXT,
        cohere_latency_ms   INTEGER,
        total_latency_ms    INTEGER NOT NULL,
        result_count        INTEGER NOT NULL,
        node_id             TEXT NOT NULL,
        correlation_id      TEXT
    )
"""

# One server worker process: imports the writer, says READY, waits for GO,
# then runs the SQLite backend's schema setup and reports every WARNING the
# writer logged plus any exception, as one JSON line.
_SCHEMA_SETUP_WORKER = """
import json, logging, sys
from code_indexer.server.services import search_event_log_writer as w

messages = []

class _Capture(logging.Handler):
    def emit(self, record):
        messages.append(record.getMessage())

w.logger.addHandler(_Capture(level=logging.WARNING))
w.logger.setLevel(logging.WARNING)
print("READY", flush=True)
sys.stdin.readline()
try:
    w.SearchEventLogSqliteBackend(sys.argv[1])
    error = None
except BaseException as exc:
    error = repr(exc)
print(json.dumps({"warnings": messages, "error": error}), flush=True)
"""

_SEL_INDEXES = {"idx_sel_timestamp", "idx_sel_user", "idx_sel_user_ts"}


def _sqlite_columns_and_indexes(db_path: str) -> Any:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(search_event_log)")}
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(search_event_log)")}
    finally:
        conn.close()
    return columns, indexes


class TestExistingTablesGainTheColumn:
    def test_sqlite_table_created_by_the_old_ddl_gains_the_column(self, tmp_path):
        import sqlite3

        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )

        db_path = str(tmp_path / "cidx_server.db")
        conn = sqlite3.connect(db_path)
        conn.execute(_PRE_2114_SQLITE_DDL)
        conn.execute(_OLD_ROW)
        conn.commit()
        conn.close()

        backend = SearchEventLogSqliteBackend(db_path)
        backend.insert_batch([_record("new query")])

        events, _ = backend.query(limit=10)
        flags = {e["query_text"]: e["query_text_truncated"] for e in events}
        assert flags == {"old query": None, "new query": 1}

    def test_concurrent_processes_add_the_column_without_failing_schema_setup(
        self, tmp_path
    ):
        """Several worker processes start on a database created by the old
        DDL. Every one reads table_info before any has added the column (the
        test holds the write lock with the column added but uncommitted until
        all have read), so each then races to ADD COLUMN. None may log a
        schema failure, and the rest of the setup (indexes) must run."""
        import json
        import os
        import sqlite3
        import subprocess
        import sys

        db_path = str(tmp_path / "cidx_server.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(_PRE_2114_SQLITE_DDL)
        conn.execute(_OLD_ROW)
        conn.commit()
        conn.close()

        src = str(Path(__file__).parents[4] / "src")
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = os.pathsep.join(
            p for p in (src, os.environ.get("PYTHONPATH", "")) if p
        )
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", _SCHEMA_SETUP_WORKER, db_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                env=child_env,
            )
            for _ in range(4)
        ]
        holder = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        try:
            for worker in workers:
                assert worker.stdout is not None
                assert worker.stdout.readline().strip() == "READY"
            holder.execute("BEGIN IMMEDIATE")
            holder.execute(
                "ALTER TABLE search_event_log ADD COLUMN query_text_truncated INTEGER"
            )
            for worker in workers:
                assert worker.stdin is not None
                worker.stdin.write("GO\n")
                worker.stdin.flush()
            time.sleep(1.0)  # every worker reads table_info, then waits on ALTER
            holder.execute("COMMIT")
            reports = []
            for worker in workers:
                out, _ = worker.communicate(timeout=60)
                reports.append(json.loads(out.strip().splitlines()[-1]))
        finally:
            holder.close()
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait(timeout=10)

        assert reports == [{"warnings": [], "error": None}] * 4
        columns, indexes = _sqlite_columns_and_indexes(db_path)
        assert "query_text_truncated" in columns
        assert _SEL_INDEXES <= indexes

    def test_fresh_database_schema_creates_the_column(self, tmp_path):
        import sqlite3

        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = str(tmp_path / "cidx_server.db")
        DatabaseSchema(db_path).initialize_database()

        conn = sqlite3.connect(db_path)
        try:
            info = list(conn.execute("PRAGMA table_info(search_event_log)"))
        finally:
            conn.close()
        name, col_type, not_null = info[-1][1], info[-1][2], info[-1][3]
        assert (name, col_type, not_null) == ("query_text_truncated", "INTEGER", 0)

    def test_alter_failure_with_the_column_still_absent_is_reported(
        self, tmp_path, caplog
    ):
        """An ADD COLUMN failure is tolerated only when the column now exists.
        Here search_event_log is a view: the column never appears, so the
        ALTER's own error is the reported schema failure (not swallowed and
        followed by a later, unrelated index failure)."""
        import logging
        import sqlite3

        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )

        db_path = str(tmp_path / "cidx_server.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE VIEW search_event_log AS SELECT 1 AS id")
        conn.commit()
        conn.close()

        with caplog.at_level(logging.WARNING):
            SearchEventLogSqliteBackend(db_path)

        failures = [
            r.getMessage() for r in caplog.records if "schema setup" in r.getMessage()
        ]
        assert len(failures) == 1
        assert "Cannot add a column to a view" in failures[0]

    @pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    def test_postgres_migration_073_adds_the_column(self):
        import uuid

        import psycopg
        from psycopg.conninfo import make_conninfo

        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogPostgresBackend,
        )
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from tests.unit.server.services.search_event_real_stores import TEST_DSN

        [migration] = sorted(_MIGRATIONS.glob("073_*.sql"))
        schema = f"t_sel_{uuid.uuid4().hex[:12]}"
        dsn = make_conninfo(TEST_DSN, options=f"-csearch_path={schema}")
        with psycopg.connect(TEST_DSN, autocommit=True) as admin:
            admin.execute(f"CREATE SCHEMA {schema}")
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute((_MIGRATIONS / "030_search_event_log.sql").read_text())
                conn.execute(_OLD_ROW)
                conn.execute(migration.read_text())
                conn.execute(migration.read_text())  # idempotent re-run
            pool = ConnectionPool(dsn, max_size=2)
            try:
                backend = SearchEventLogPostgresBackend(pool)
                backend.insert_batch([_record("new query")])
                events, _ = backend.query(limit=10)
            finally:
                pool.close()
        finally:
            with psycopg.connect(TEST_DSN, autocommit=True) as admin:
                admin.execute(f"DROP SCHEMA {schema} CASCADE")

        flags = {e["query_text"]: e["query_text_truncated"] for e in events}
        assert flags == {"old query": None, "new query": True}


def _overflowed_writer(backend: Any, dropped: int, clock: Any = None) -> Any:
    """A real writer with a 3-event queue and no drain running, fed 3 +
    `dropped` events: the queue fills, then every further event is dropped."""
    from code_indexer.server.services.search_event_log_writer import (
        SearchEventLogWriter,
    )
    from tests.unit.server.services.search_event_real_stores import make_record

    kwargs = {} if clock is None else {"clock": clock}
    writer = SearchEventLogWriter(backend, maxsize=3, **kwargs)
    for i in range(3 + dropped):
        writer.enqueue(make_record(query_text=f"query {i}"))
    return writer


class TestDroppedEventsAreCounted:
    def test_overflow_counts_every_dropped_event(self, env):
        writer = _overflowed_writer(env.rec.store.backend, dropped=4)

        assert writer.drop_snapshot().dropped == 4

        writer.start()
        writer.stop()  # the thread's final drain writes what was queued
        events, _ = env.rec.store.backend.query(limit=10)
        assert sorted(e["query_text"] for e in events) == [
            "query 0",
            "query 1",
            "query 2",
        ]


class TestDropsAreCountedInOtel:
    def test_each_drop_increments_the_otel_counter(self, tmp_path):
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )
        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics_singleton,
            find_metric,
        )

        backend = SearchEventLogSqliteBackend(str(tmp_path / "events.db"))
        with active_application_metrics_singleton() as (_metrics, reader):
            _overflowed_writer(backend, dropped=3)
            metric = find_metric(reader, "cidx.search_event_log.dropped_events")

        assert metric is not None
        assert sum(dp.value for dp in metric.data.data_points) == 3


class _Clock:
    """A monotonic clock the test advances."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def app_writer(monkeypatch, tmp_path):
    """Install a writer on the server app's state, as lifespan does."""
    from code_indexer.server import app as app_package

    def _install(writer: Any) -> None:
        monkeypatch.setattr(
            app_package.app.state, "search_event_log_writer", writer, raising=False
        )

    return _install


def _health(tmp_path: Path) -> Any:
    from code_indexer.server.services.health_service import HealthCheckService

    service = HealthCheckService()
    service.database_url = f"sqlite:///{tmp_path / 'cidx_server.db'}"
    return service.get_system_health()


def _drop_reasons(health: Any) -> List[str]:
    return [r for r in health.failure_reasons if "search event" in r.lower()]


class TestDropsAreVisibleThroughHealth:
    def test_health_reports_drop_count_and_degrades_while_recent(
        self, app_writer, tmp_path
    ):
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )

        backend = SearchEventLogSqliteBackend(str(tmp_path / "events.db"))
        app_writer(_overflowed_writer(backend, dropped=0))
        quiet = _health(tmp_path)
        assert quiet.search_events.events_dropped_since_start == 0
        assert _drop_reasons(quiet) == []

        app_writer(_overflowed_writer(backend, dropped=2))
        health = _health(tmp_path)

        assert health.search_events.events_dropped_since_start == 2
        [reason] = _drop_reasons(health)
        assert "2" in reason
        assert "server process" in reason
        assert health.status.value != "healthy"

    def test_reason_clears_after_the_window_but_the_count_stays(
        self, app_writer, tmp_path
    ):
        from code_indexer.server.services.search_event_health import (
            SEARCH_EVENT_DROP_WINDOW_SECONDS,
        )
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )

        clock = _Clock()
        backend = SearchEventLogSqliteBackend(str(tmp_path / "events.db"))
        app_writer(_overflowed_writer(backend, dropped=1, clock=clock))
        clock.now += SEARCH_EVENT_DROP_WINDOW_SECONDS + 1

        health = _health(tmp_path)

        assert health.search_events.events_dropped_since_start == 1
        assert _drop_reasons(health) == []

    def test_rest_health_front_door_reports_the_drop_count(self, app_writer, tmp_path):
        from fastapi.testclient import TestClient

        from code_indexer.server.app import create_app
        from code_indexer.server.auth.dependencies import get_current_user
        from code_indexer.server.auth.user_manager import UserRole
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )

        backend = SearchEventLogSqliteBackend(str(tmp_path / "events.db"))
        app_writer(_overflowed_writer(backend, dropped=3))
        app = create_app()
        admin = make_user("health-admin")
        admin.role = UserRole.ADMIN
        app.dependency_overrides[get_current_user] = lambda: admin
        try:
            client = TestClient(app)
            system = client.get("/api/system/health").json()
            health = client.get("/health").json()
        finally:
            app.dependency_overrides.clear()

        assert system["search_events"]["events_dropped_since_start"] == 3
        assert any("search event" in r.lower() for r in system["failure_reasons"])
        assert health["search_events"]["events_dropped_since_start"] == 3


def _export_page_html() -> str:
    from unittest.mock import MagicMock, patch

    from fastapi.testclient import TestClient

    from code_indexer.server.app import app

    session = MagicMock(role="admin", username="admin", csrf_token="t")
    session_manager = MagicMock()
    session_manager.get_session.return_value = session
    session_manager._serializer.secret_key = "unit-test-secret"
    session_manager._serializer.secret_keys = ["unit-test-secret"]
    with patch(
        "code_indexer.server.web.routes.get_session_manager",
        return_value=session_manager,
    ):
        return str(TestClient(app).get("/admin/analytics-export").text)


class TestExportPageStatesDrops:
    def test_export_page_states_drops_of_the_serving_process(
        self, app_writer, tmp_path
    ):
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogSqliteBackend,
        )

        backend = SearchEventLogSqliteBackend(str(tmp_path / "events.db"))
        app_writer(_overflowed_writer(backend, dropped=0))
        assert "search-event-drops" not in _export_page_html()

        app_writer(_overflowed_writer(backend, dropped=7))
        html = _export_page_html()

        assert 'id="search-event-drops"' in html
        assert "7 search events" in html
        # The count is per server PROCESS (each worker has its own writer).
        assert "This server process" in html
        assert "This server node" not in html


class TestExportCarriesTheMarker:
    def test_export_has_a_truncation_column_after_query_text(self, env, tmp_path):
        import uuid

        import openpyxl

        from code_indexer.server.services.query_analytics_export_service import (
            QueryAnalyticsExportService,
        )
        from tests.unit.server.services.search_event_real_stores import make_record

        env.rec.store.bulk_insert([make_record(query_text="old query")])
        env.rec.store.backend.insert_batch(
            [
                make_record(query_text="cut query", query_text_truncated=True),
                make_record(query_text="short query", query_text_truncated=False),
            ]
        )
        service = QueryAnalyticsExportService(
            env.rec.store.export_backend, str(tmp_path / "golden-repos")
        )
        export_id = str(uuid.uuid4())
        service.run_export(export_id, {}, "admin", 30, env.rec.store.backend)

        sheet = openpyxl.load_workbook(service.export_path(export_id)).active
        header, *rows = [list(r) for r in sheet.iter_rows(values_only=True)]
        column = header.index("query_text_truncated")
        assert header[column - 1] == "query_text"
        flags = {row[header.index("query_text")]: row[column] for row in rows}
        assert flags == {
            "old query": None,
            "cut query": True,
            "short query": False,
        }
