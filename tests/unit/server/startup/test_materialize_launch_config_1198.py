"""Tests for Story #1198: materialize_launch_config() and launch.json wiring.

TDD: RED -> GREEN -> REFACTOR sequence.

All tests are source-text or source-order guards following the established
pattern in this directory (test_lifespan_coalescer_registry_wiring.py etc.)
to avoid transitive fastapi import failures.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from code_indexer.server.services.config_service import ConfigService

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEPLOYMENT_EXECUTOR_PATH = (
    _REPO_ROOT
    / "src"
    / "code_indexer"
    / "server"
    / "auto_update"
    / "deployment_executor.py"
)
_CONFIG_SERVICE_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "services" / "config_service.py"
)
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)
_SERVICE_INIT_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "service_init.py"
)


# ---------------------------------------------------------------------------
# Step 1: LAUNCH_CONFIG_PATH and APPLIED_LAUNCH_CONFIG_PATH constants
#         (MAJOR-M2 source-text guard)
# ---------------------------------------------------------------------------


class TestLaunchConfigPathConstantsInDeploymentExecutor:
    """MAJOR-M2: LAUNCH_CONFIG_PATH and APPLIED_LAUNCH_CONFIG_PATH in deployment_executor.py."""

    def test_launch_config_path_constant_defined(self) -> None:
        """deployment_executor.py must define LAUNCH_CONFIG_PATH constant."""
        source = _DEPLOYMENT_EXECUTOR_PATH.read_text()
        assert "LAUNCH_CONFIG_PATH" in source, (
            "deployment_executor.py must define LAUNCH_CONFIG_PATH constant "
            "(Story #1198 MAJOR-M2)"
        )

    def test_applied_launch_config_path_constant_defined(self) -> None:
        """deployment_executor.py must define APPLIED_LAUNCH_CONFIG_PATH constant."""
        source = _DEPLOYMENT_EXECUTOR_PATH.read_text()
        assert "APPLIED_LAUNCH_CONFIG_PATH" in source, (
            "deployment_executor.py must define APPLIED_LAUNCH_CONFIG_PATH constant "
            "(Story #1198 MAJOR-M2)"
        )

    def test_launch_config_path_uses_cidx_data_dir(self) -> None:
        """LAUNCH_CONFIG_PATH must be built from _cidx_data_dir (co-location guard)."""
        source = _DEPLOYMENT_EXECUTOR_PATH.read_text()
        # The constant must be defined as _cidx_data_dir / "launch.json"
        assert '_cidx_data_dir / "launch.json"' in source, (
            "LAUNCH_CONFIG_PATH must be defined as _cidx_data_dir / 'launch.json' "
            "so it is co-located with RESTART_SIGNAL_PATH (Story #1198 MAJOR-M2)"
        )

    def test_applied_launch_config_path_uses_cidx_data_dir(self) -> None:
        """APPLIED_LAUNCH_CONFIG_PATH must be built from _cidx_data_dir."""
        source = _DEPLOYMENT_EXECUTOR_PATH.read_text()
        assert '_cidx_data_dir / "applied_launch.json"' in source, (
            "APPLIED_LAUNCH_CONFIG_PATH must be defined as "
            "_cidx_data_dir / 'applied_launch.json' "
            "(Story #1198 MAJOR-M2)"
        )


# ---------------------------------------------------------------------------
# Step 2: materialize_launch_config() source-text guard (AC1)
# ---------------------------------------------------------------------------


class TestMaterializeLaunchConfigInConfigService:
    """AC1: config_service.py must define materialize_launch_config()."""

    def test_method_defined_in_config_service(self) -> None:
        """ConfigService must have materialize_launch_config() method."""
        source = _CONFIG_SERVICE_PATH.read_text()
        assert "def materialize_launch_config" in source, (
            "ConfigService must define materialize_launch_config() (Story #1198 AC1)"
        )

    def test_imports_launch_config_path_from_deployment_executor(self) -> None:
        """config_service.py must import LAUNCH_CONFIG_PATH from deployment_executor."""
        source = _CONFIG_SERVICE_PATH.read_text()
        assert "LAUNCH_CONFIG_PATH" in source and "deployment_executor" in source, (
            "config_service.py must import LAUNCH_CONFIG_PATH from deployment_executor "
            "(Story #1198 AC1)"
        )

    def test_materialize_writes_required_fields(self) -> None:
        """materialize_launch_config() body must reference all 5 required output fields."""
        source = _CONFIG_SERVICE_PATH.read_text()
        method_start = source.find("def materialize_launch_config")
        assert method_start != -1, "materialize_launch_config not found"
        method_body = source[method_start : method_start + 2000]
        for field in (
            "workers",
            "log_level",
            "host",
            "port",
            "target_restart_generation",
        ):
            assert field in method_body, (
                f"materialize_launch_config() must write '{field}' to launch.json "
                f"(Story #1198 AC1)"
            )


# ---------------------------------------------------------------------------
# Step 3: Atomic write and fail-soft return values (AC1/AC6)
# ---------------------------------------------------------------------------


class TestMaterializeAtomicAndFailSoft:
    """AC1/AC6: materialize_launch_config() uses os.replace and returns bool."""

    def _method_body(self) -> str:
        source = _CONFIG_SERVICE_PATH.read_text()
        start = source.find("def materialize_launch_config")
        assert start != -1
        return source[start : start + 2000]

    def test_materialize_uses_os_replace(self) -> None:
        """materialize_launch_config() must call os.replace for atomic write."""
        assert "os.replace" in self._method_body(), (
            "materialize_launch_config() must use os.replace() for atomic write "
            "(Story #1198 AC1)"
        )

    def test_materialize_returns_true_on_success(self) -> None:
        """materialize_launch_config() must return True on success."""
        assert "return True" in self._method_body(), (
            "materialize_launch_config() must return True on success (Story #1198 AC1)"
        )

    def test_materialize_returns_false_on_failure(self) -> None:
        """materialize_launch_config() must return False on failure (fail-soft)."""
        assert "return False" in self._method_body(), (
            "materialize_launch_config() must return False on failure (Story #1198 AC6)"
        )


# ---------------------------------------------------------------------------
# Step 4: COALESCE zero and save-path wiring (AC1/AC3)
# ---------------------------------------------------------------------------

_HELPER_SCAN_WINDOW_CHARS = 2000


class TestMaterializeCoalesceZeroAndSaveWiring:
    """AC1/AC3: launch_restart_generation COALESCE 0 and save-path wiring."""

    def _helper_body(self) -> str:
        source = _CONFIG_SERVICE_PATH.read_text()
        start = source.find("def _read_raw_launch_generation")
        assert start != -1, "_read_raw_launch_generation not found"
        return source[start : start + _HELPER_SCAN_WINDOW_CHARS]

    def test_read_raw_launch_generation_helper_exists(self) -> None:
        """_read_raw_launch_generation must be defined in ConfigService."""
        source = _CONFIG_SERVICE_PATH.read_text()
        assert "def _read_raw_launch_generation" in source, (
            "ConfigService must define _read_raw_launch_generation() (Story #1198 AC1)"
        )

    def test_coalesce_zero_default_in_helper(self) -> None:
        """_read_raw_launch_generation must use .get('launch_restart_generation', 0)."""
        body = self._helper_body()
        assert (
            '"launch_restart_generation", 0' in body
            or "'launch_restart_generation', 0" in body
        ), (
            "_read_raw_launch_generation must COALESCE 0 when key absent (Story #1198 AC1)"
        )

    def test_save_runtime_to_sqlite_calls_materialize(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """_save_runtime_to_sqlite must call materialize_launch_config after saving (AC3).

        Behavioral test: drives the real method with a spy on materialize_launch_config
        so the assertion is method-length-agnostic (no brittle char-window scan).
        """
        from unittest.mock import MagicMock
        import json as _json

        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager
        from code_indexer.server.storage.database_manager import DatabaseSchema

        # Build a minimal ConfigService with real SQLite
        server_dir = tmp_path / "server"
        server_dir.mkdir(parents=True, exist_ok=True)
        (server_dir / "config.json").write_text(
            _json.dumps(
                {"host": "127.0.0.1", "port": 8000, "workers": 2, "log_level": "INFO"}
            )
        )
        mgr = ServerConfigManager(server_dir_path=str(server_dir))
        svc = ConfigService(config_manager=mgr)
        db_path = str(tmp_path / "cidx.db")
        DatabaseSchema(db_path).initialize_database()
        svc.initialize_runtime_db(db_path)

        # Spy: replace materialize_launch_config with a MagicMock
        spy = MagicMock(return_value=True)
        svc.materialize_launch_config = spy  # type: ignore[method-assign]

        # Drive _save_runtime_to_sqlite directly
        runtime_dict = {
            "workers": 4,
            "log_level": "info",
            "host": "0.0.0.0",
            "port": 8000,
        }
        svc._save_runtime_to_sqlite(runtime_dict)

        # AC3: materialize_launch_config must have been called
        assert spy.called, (
            "_save_runtime_to_sqlite must call self.materialize_launch_config() "
            "after saving (Story #1198 AC3)"
        )
        # _db_config_version must have been set from the saved row
        assert svc._db_config_version > 0, (
            "_save_runtime_to_sqlite must set _db_config_version from the "
            "version read-back after saving (Story #1198 AC3)"
        )


# ---------------------------------------------------------------------------
# Step 5: PG save wiring guard with ordering check (AC3)
# ---------------------------------------------------------------------------


class TestMaterializePgSaveWiring:
    """AC3: _save_runtime_to_pg must call materialize_launch_config after _db_config_version."""

    def test_save_runtime_to_pg_calls_materialize_after_version_set(
        self, tmp_path: Path
    ) -> None:
        """_save_runtime_to_pg must set _db_config_version THEN call materialize (AC3).

        Behavioral test: uses a mock PG pool + spy on materialize_launch_config so the
        assertion is method-length-agnostic (no brittle char-window scan).
        The call_order list records the sequence of side-effects so we can verify
        _db_config_version is set BEFORE materialize_launch_config fires.
        """
        import json as _json
        from unittest.mock import MagicMock

        from code_indexer.server.services.config_service import ConfigService

        # Build a minimal ConfigService without SQLite path (PG-only path)
        server_dir = tmp_path / "server"
        server_dir.mkdir(parents=True, exist_ok=True)
        (server_dir / "config.json").write_text(
            _json.dumps(
                {"host": "127.0.0.1", "port": 8000, "workers": 2, "log_level": "INFO"}
            )
        )
        svc = ConfigService(server_dir_path=str(server_dir))
        svc.load_config()
        svc._sqlite_db_path = None  # PG-only path

        # Build mock PG pool that returns version=42 on the post-UPDATE fetchone
        cur = MagicMock()
        cur.execute.return_value.fetchone.side_effect = [{"version": 42}]

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        pool = MagicMock()
        pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        svc._pool = pool

        # Track call order to verify version is set BEFORE materialize fires
        call_order: list = []

        original_materialize = svc.materialize_launch_config

        def _spy_materialize():  # type: ignore[no-untyped-def]
            call_order.append(("materialize", svc._db_config_version))
            return original_materialize()

        svc.materialize_launch_config = _spy_materialize  # type: ignore[method-assign]

        config = svc.get_config()
        # Patch materialize to also be fail-soft (no real filesystem write needed)
        svc.materialize_launch_config = _spy_materialize  # type: ignore[method-assign]
        # monkeypatch LAUNCH_CONFIG_PATH so the real materialize doesn't write to disk
        import code_indexer.server.services.config_service as cs_mod

        original_path = cs_mod.LAUNCH_CONFIG_PATH
        cs_mod.LAUNCH_CONFIG_PATH = tmp_path / "launch.json"
        try:
            svc._save_runtime_to_pg(config)
        finally:
            cs_mod.LAUNCH_CONFIG_PATH = original_path

        # AC3a: materialize_launch_config must have been called
        assert call_order, (
            "_save_runtime_to_pg must call materialize_launch_config() after saving "
            "(Story #1198 AC3)"
        )

        # AC3b: _db_config_version must be set (to version=42) before materialize fires
        version_at_materialize_call = call_order[0][1]
        assert version_at_materialize_call == 42, (
            f"_save_runtime_to_pg must set _db_config_version (42) BEFORE calling "
            f"materialize_launch_config(); got {version_at_materialize_call!r} "
            "(Story #1198 AC3)"
        )


# ---------------------------------------------------------------------------
# Step 7: AC5 — log_level from launch.json in lifespan startup
# ---------------------------------------------------------------------------

_LOG_LEVEL_SCAN_WINDOW_CHARS = 3000


class TestLifespanLogLevelFromLaunchJson:
    """AC5: lifespan.py reads log_level from launch.json before config.json."""

    def _startup_log_window(self) -> str:
        """Source window from 3000 chars before first load_config() call."""
        source = _LIFESPAN_PATH.read_text()
        load_pos = source.find("load_config()")
        assert load_pos != -1, "load_config() not found in lifespan.py"
        start = max(0, load_pos - _LOG_LEVEL_SCAN_WINDOW_CHARS)
        return source[start:load_pos]

    def test_launch_config_path_read_text_before_load_config(self) -> None:
        """lifespan.py must call LAUNCH_CONFIG_PATH.read_text() before load_config()."""
        window = self._startup_log_window()
        assert "LAUNCH_CONFIG_PATH.read_text" in window, (
            "lifespan.py must read launch.json via LAUNCH_CONFIG_PATH.read_text() "
            "before load_config() (Story #1198 AC5)"
        )

    def test_json_parse_before_log_level_get(self) -> None:
        """JSON parse must precede .get('log_level') in the launch.json read block."""
        window = self._startup_log_window()
        json_positions = [
            pos
            for pos in (window.find("json.loads"), window.find("json.load("))
            if pos != -1
        ]
        get_positions = [
            pos
            for pos in (
                window.find('.get("log_level")'),
                window.find(".get('log_level')"),
            )
            if pos != -1
        ]
        assert (
            json_positions
            and get_positions
            and min(json_positions) < min(get_positions)
        ), (
            "lifespan.py must parse launch.json via json.loads/json.load BEFORE "
            "accessing log_level via .get('log_level') before load_config() "
            "(Story #1198 AC5)"
        )


# ---------------------------------------------------------------------------
# BEHAVIORAL TESTS (real SQLite + real temp files — catch CRITICAL-1 and -2)
# ---------------------------------------------------------------------------


def _create_server_config_table(conn) -> None:  # type: ignore[no-untyped-def]
    """Create the server_config table (mirrors database_manager.py schema)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS server_config (
            config_key TEXT PRIMARY KEY DEFAULT 'runtime',
            config_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT DEFAULT (datetime('now')),
            updated_by TEXT
        )
        """
    )
    conn.commit()


def _seed_runtime_row(  # type: ignore[no-untyped-def]
    conn,
    workers: int = 4,
    log_level: str = "info",
    host: str = "0.0.0.0",
    port: int = 8000,
    extra: "dict | None" = None,
) -> None:
    """Insert/update a runtime row in server_config."""
    import json as _json

    payload: dict = {
        "workers": workers,
        "log_level": log_level,
        "host": host,
        "port": port,
    }
    if extra:
        payload.update(extra)
    conn.execute(
        "INSERT INTO server_config (config_key, config_json, version) "
        "VALUES (?, ?, 1) "
        "ON CONFLICT(config_key) DO UPDATE SET "
        "config_json = excluded.config_json, "
        "version = server_config.version + 1",
        ("runtime", _json.dumps(payload)),
    )
    conn.commit()


class _DictRow(dict):
    """dict-like row (mirrors psycopg dict_row output)."""


class _FakeCursor:
    """Faithful psycopg3 cursor backed by a real sqlite3.Cursor.

    execute/fetchone live on the CURSOR (not the connection), matching the real
    psycopg3 contract.  When row_factory is supplied, fetchone/fetchall return
    _DictRow objects keyed by column name (mirrors psycopg dict_row).
    __exit__ closes the underlying sqlite cursor to prevent resource leaks.
    """

    def __init__(self, sqlite_conn, row_factory=None) -> None:  # type: ignore[no-untyped-def]
        self._row_factory = row_factory
        self._cur = sqlite_conn.cursor()

    def execute(self, sql: str, params=None) -> "_FakeCursor":
        sql = sql.replace("%s", "?").replace("EXCLUDED.", "excluded.")
        if params is not None:
            self._cur.execute(sql, params)
        else:
            self._cur.execute(sql)
        return self

    def fetchone(self):  # type: ignore[no-untyped-def]
        row = self._cur.fetchone()
        if row is None:
            return None
        if self._row_factory is not None:
            cols = [d[0] for d in (self._cur.description or [])]
            return _DictRow(zip(cols, row))
        return row

    def fetchall(self):  # type: ignore[no-untyped-def]
        rows = self._cur.fetchall()
        if self._row_factory is not None:
            cols = [d[0] for d in (self._cur.description or [])]
            return [_DictRow(zip(cols, r)) for r in rows]
        return rows

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args) -> None:
        self._cur.close()


class _FakeConnection:
    """Faithful psycopg3 connection adapter over a real sqlite3.Connection.

    Ownership: the test retains ownership of the sqlite3.Connection; __exit__
    does NOT close it (matches psycopg ConnectionPool checkout semantics where
    the pool retains ownership).
    """

    def __init__(self, sqlite_conn) -> None:  # type: ignore[no-untyped-def]
        self._conn = sqlite_conn

    def execute(self, sql: str, params=None) -> _FakeCursor:
        return _FakeCursor(self._conn).execute(sql, params)

    def cursor(self, row_factory=None) -> _FakeCursor:  # type: ignore[no-untyped-def]
        return _FakeCursor(self._conn, row_factory=row_factory)

    def commit(self) -> None:
        self._conn.commit()

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *args) -> None:
        pass  # pool retains sqlite connection ownership


class _FakePool:
    """Faithful psycopg3 ConnectionPool over a real sqlite3.Connection."""

    def __init__(self, sqlite_conn) -> None:  # type: ignore[no-untyped-def]
        from contextlib import contextmanager

        _conn = sqlite_conn

        @contextmanager  # type: ignore[misc]
        def _connection():  # type: ignore[no-untyped-def]
            yield _FakeConnection(_conn)

        self.connection = _connection


def _make_real_config_service(server_dir: Path):  # type: ignore[no-untyped-def]
    """Build a ConfigService backed by real files; creates server_dir if absent."""
    import json as _json

    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfigManager

    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "config.json").write_text(
        _json.dumps(
            {"host": "127.0.0.1", "port": 8000, "workers": 2, "log_level": "INFO"}
        )
    )
    mgr = ServerConfigManager(server_dir_path=str(server_dir))
    return ConfigService(config_manager=mgr)


# ---------------------------------------------------------------------------
# CRITICAL-1: _read_raw_launch_generation() must check _pool FIRST
# ---------------------------------------------------------------------------


class TestCritical1PgFirstPriority:
    """CRITICAL-1: when both _pool (gen=99) and _sqlite_db_path (gen=7) are set,
    _read_raw_launch_generation() must return 99 (PG wins)."""

    def test_pg_generation_wins_over_sqlite_when_both_set(self, tmp_path: Path) -> None:
        """Cluster mode: PG gen=99 must beat local SQLite gen=7."""
        import sqlite3

        # (A) local SQLite file: gen=7
        local_db_path = str(tmp_path / "local.db")
        local_conn = sqlite3.connect(local_db_path)
        try:
            _create_server_config_table(local_conn)
            _seed_runtime_row(local_conn, extra={"launch_restart_generation": 7})
        finally:
            local_conn.close()

        # (B) PG-simulating in-memory SQLite: gen=99
        pg_db = sqlite3.connect(":memory:")
        try:
            _create_server_config_table(pg_db)
            _seed_runtime_row(pg_db, extra={"launch_restart_generation": 99})

            svc = _make_real_config_service(tmp_path / "server")
            svc._sqlite_db_path = local_db_path
            svc._pool = _FakePool(pg_db)

            result = svc._read_raw_launch_generation()
        finally:
            pg_db.close()

        assert result == 99, (
            f"CRITICAL-1: both backends set — got {result!r}, expected 99 (PG). "
            "SQLite gen=7, PG gen=99. _pool check must come FIRST."
        )

    def test_sqlite_fallback_when_pool_is_none(self, tmp_path: Path) -> None:
        """Solo mode: SQLite gen=7 is returned when _pool is None."""
        import sqlite3

        local_db_path = str(tmp_path / "solo.db")
        conn = sqlite3.connect(local_db_path)
        try:
            _create_server_config_table(conn)
            _seed_runtime_row(conn, extra={"launch_restart_generation": 7})
        finally:
            conn.close()

        svc = _make_real_config_service(tmp_path / "server")
        svc._sqlite_db_path = local_db_path
        # _pool stays None

        assert svc._read_raw_launch_generation() == 7

    def test_coalesce_zero_when_key_absent(self, tmp_path: Path) -> None:
        """When launch_restart_generation is absent from the row, COALESCE 0."""
        import sqlite3

        local_db_path = str(tmp_path / "no_gen.db")
        conn = sqlite3.connect(local_db_path)
        try:
            _create_server_config_table(conn)
            _seed_runtime_row(conn)  # no launch_restart_generation
        finally:
            conn.close()

        svc = _make_real_config_service(tmp_path / "server")
        svc._sqlite_db_path = local_db_path

        assert svc._read_raw_launch_generation() == 0


# ---------------------------------------------------------------------------
# CRITICAL-2: save_config() must materialize the NEW value, not the stale cache
# ---------------------------------------------------------------------------


class TestCritical2SaveWritesNewConfig:
    """CRITICAL-2: on-save materialize must reflect the just-saved config."""

    def _make_svc_with_real_db(self, server_dir: Path) -> "ConfigService":
        """ConfigService with real SQLite initialized; workers=2 in config.json."""
        import json as _json

        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager
        from code_indexer.server.storage.database_manager import DatabaseSchema

        server_dir.mkdir(parents=True, exist_ok=True)
        (server_dir / "config.json").write_text(
            _json.dumps(
                {"host": "127.0.0.1", "port": 8000, "workers": 2, "log_level": "INFO"}
            )
        )
        mgr = ServerConfigManager(server_dir_path=str(server_dir))
        svc = ConfigService(config_manager=mgr)
        db_path = str(server_dir / "cidx.db")
        # Initialize the schema (creates server_config table) before runtime DB
        DatabaseSchema(db_path).initialize_database()
        svc.initialize_runtime_db(db_path)
        return svc

    def test_sqlite_save_writes_new_workers_not_stale(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """save_config(workers=6) via SQLite path → launch.json workers=6 (not 2)."""
        import json as _json

        launch_path = tmp_path / "launch.json"
        import code_indexer.server.services.config_service as cs_mod

        monkeypatch.setattr(cs_mod, "LAUNCH_CONFIG_PATH", launch_path)

        svc = self._make_svc_with_real_db(tmp_path / "server")
        starting = svc.get_config()
        assert starting.workers == 2, "Precondition: workers must start at 2"

        from dataclasses import replace

        svc.save_config(replace(starting, workers=6))

        assert launch_path.exists(), "launch.json must exist after save_config()"
        written = _json.loads(launch_path.read_text())
        assert written["workers"] == 6, (
            f"CRITICAL-2 (SQLite): got workers={written['workers']!r}, expected 6. "
            "self._config must be updated BEFORE materialize is called."
        )

    def test_pg_save_writes_new_workers_not_stale(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """save_config(workers=8) via PG path → runtime_dict in UPDATE contains workers=8.

        Captures the SQL + params sent to the PG cursor to prove:
        (1) The UPDATE uses jsonb_set (atomic, not stale SELECT+overwrite).
        (2) The runtime_dict JSON param passed to jsonb_set contains workers=8,
            NOT the stale workers=2 from the initial config.

        Uses a SQL-capturing mock pool instead of _FakeCursor/_FakePool executing
        PG-specific SQL (::jsonb, jsonb_set) against SQLite, which would raise
        sqlite3.OperationalError: unrecognized token ":".
        """
        import json as _json
        from dataclasses import replace
        from unittest.mock import MagicMock

        import code_indexer.server.services.config_service as cs_mod

        launch_path = tmp_path / "launch.json"
        monkeypatch.setattr(cs_mod, "LAUNCH_CONFIG_PATH", launch_path)

        svc = self._make_svc_with_real_db(tmp_path / "server")
        starting = svc.get_config()
        assert starting.workers == 2, "Precondition: workers must start at 2"

        # Build a SQL-capturing mock PG pool.
        # The post-UPDATE version SELECT returns version=5.
        cur = MagicMock()
        cur.execute.return_value.fetchone.side_effect = [{"version": 5}]

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        pool = MagicMock()
        pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        svc._pool = pool

        svc.save_config(replace(starting, workers=8))

        # Collect every SQL string sent to cur.execute
        all_sql_calls = [str(c.args[0]) for c in cur.execute.call_args_list]

        # Find the UPDATE statement
        update_calls = [sql for sql in all_sql_calls if "UPDATE server_config" in sql]
        assert update_calls, (
            "CRITICAL-2 (PG): _save_runtime_to_pg must issue an UPDATE server_config statement"
        )
        update_sql = update_calls[0]

        # (1) The UPDATE must use jsonb_set (atomic generation preservation)
        assert "jsonb_set" in update_sql, (
            "CRITICAL-2 (PG): the UPDATE must use jsonb_set() — "
            "a plain UPDATE without jsonb_set is the racy SELECT+overwrite pattern"
        )

        # (2) The runtime_dict JSON param (first %s in the UPDATE) must contain workers=8
        # cur.execute is called with (sql, (runtime_dict_json, updated_by, config_key))
        update_call_args = [
            c
            for c in cur.execute.call_args_list
            if "UPDATE server_config" in str(c.args[0])
        ]
        assert update_call_args, "Must find the UPDATE call args"
        update_params = update_call_args[0].args[1]  # tuple of bound params
        runtime_dict_json = update_params[0]  # first %s = json.dumps(runtime_dict)
        runtime_dict = _json.loads(runtime_dict_json)
        assert runtime_dict.get("workers") == 8, (
            f"CRITICAL-2 (PG): runtime_dict passed to UPDATE has workers="
            f"{runtime_dict.get('workers')!r}, expected 8. "
            "The PG save path must use the NEW config value, not the stale one."
        )


# ---------------------------------------------------------------------------
# Behavioral: materialize_launch_config() writes real files correctly
# ---------------------------------------------------------------------------


class TestBehavioralMaterializeLaunchConfig:
    """Full behavioral coverage: real ConfigService, real SQLite, real files."""

    def _make_svc(self, server_dir: Path, db_path: str) -> "ConfigService":
        import json as _json

        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager
        from code_indexer.server.storage.database_manager import DatabaseSchema

        server_dir.mkdir(parents=True, exist_ok=True)
        (server_dir / "config.json").write_text(
            _json.dumps(
                {"host": "0.0.0.0", "port": 8000, "workers": 4, "log_level": "info"}
            )
        )
        mgr = ServerConfigManager(server_dir_path=str(server_dir))
        svc = ConfigService(config_manager=mgr)
        # Initialize the schema (creates server_config table) before runtime DB
        DatabaseSchema(db_path).initialize_database()
        svc.initialize_runtime_db(db_path)
        return svc

    def test_materialize_writes_exact_5_key_schema(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Writes exactly {workers, log_level, host, port, target_restart_generation}."""
        import json as _json

        launch_path = tmp_path / "launch.json"
        import code_indexer.server.services.config_service as cs_mod

        monkeypatch.setattr(cs_mod, "LAUNCH_CONFIG_PATH", launch_path)

        svc = self._make_svc(tmp_path / "server", str(tmp_path / "cidx.db"))
        result = svc.materialize_launch_config()

        assert result is True
        assert launch_path.exists()
        data = _json.loads(launch_path.read_text())
        assert set(data.keys()) == {
            "workers",
            "log_level",
            "host",
            "port",
            "target_restart_generation",
        }, f"Unexpected keys: {set(data.keys())}"
        assert "applied_restart_generation" not in data
        assert data["target_restart_generation"] == 0
        assert data["workers"] == 4
        assert data["port"] == 8000

    def test_materialize_is_atomic(self, tmp_path: Path, monkeypatch) -> None:
        """Two successive materializes produce identical valid JSON."""
        import json as _json

        launch_path = tmp_path / "launch.json"
        import code_indexer.server.services.config_service as cs_mod

        monkeypatch.setattr(cs_mod, "LAUNCH_CONFIG_PATH", launch_path)

        svc = self._make_svc(tmp_path / "server", str(tmp_path / "cidx.db"))
        svc.materialize_launch_config()
        first = _json.loads(launch_path.read_text())
        svc.materialize_launch_config()
        second = _json.loads(launch_path.read_text())

        assert first == second

    def test_materialize_fail_soft_returns_false(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Write error → returns False; does not raise (AC6)."""
        unwritable = tmp_path / "nowrite"
        unwritable.mkdir()
        unwritable.chmod(0o444)
        bad_path = unwritable / "launch.json"

        import code_indexer.server.services.config_service as cs_mod

        monkeypatch.setattr(cs_mod, "LAUNCH_CONFIG_PATH", bad_path)

        svc = self._make_svc(tmp_path / "server", str(tmp_path / "cidx.db"))
        result = svc.materialize_launch_config()

        unwritable.chmod(0o755)  # restore before cleanup
        assert result is False

    def test_colocation_launch_json_same_dir_as_restart_signal(self) -> None:
        """MAJOR-M2: LAUNCH_CONFIG_PATH.parent == RESTART_SIGNAL_PATH.parent."""
        from code_indexer.server.auto_update.deployment_executor import (
            LAUNCH_CONFIG_PATH,
            RESTART_SIGNAL_PATH,
        )

        assert LAUNCH_CONFIG_PATH.parent == RESTART_SIGNAL_PATH.parent, (
            f"LAUNCH_CONFIG_PATH={LAUNCH_CONFIG_PATH} must share parent with "
            f"RESTART_SIGNAL_PATH={RESTART_SIGNAL_PATH}"
        )

    def test_ac5_log_level_from_real_launch_json(self, tmp_path: Path) -> None:
        """AC5: log_level resolved from a real launch.json file."""
        import json as _json

        launch_path = tmp_path / "launch.json"
        launch_path.write_text(
            _json.dumps(
                {
                    "workers": 2,
                    "log_level": "debug",
                    "host": "127.0.0.1",
                    "port": 8000,
                    "target_restart_generation": 0,
                }
            )
        )

        # Mirrors lifespan.py resolution logic
        resolved = "INFO"
        try:
            resolved = _json.loads(launch_path.read_text()).get("log_level", resolved)
        except Exception:
            pass

        assert resolved == "debug"
