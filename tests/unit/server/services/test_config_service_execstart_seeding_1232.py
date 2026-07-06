"""
TDD tests for Bug #1232 (REWORKED): host/port/workers gap-filled from the live
ExecStart ONLY at first-boot DB seed, NOT in materialize_launch_config.

Correct fix layer: _backfill_launch_keys_from_execstart() is called at first-boot
centralization in BOTH initialize_runtime_db (SQLite solo path) AND
_seed_runtime_to_pg (PG cluster first-boot path). It gap-fills ONLY keys absent
from config.json. materialize_launch_config() uses desired state (self._config) directly.

Tests use:
- Real ConfigService backed by real tmp config.json
- Real SQLite via DatabaseSchema + initialize_runtime_db (SQLite path tests)
- _FakePool/_FakeConnection backed by real SQLite (PG path test)
- Fake systemd unit file on disk (monkeypatched SYSTEMD_UNIT_DIR) — same technique
  as the existing #1198 behavioral tests; no core mocking.
- Direct DB row inspection to verify the seeded values, not just in-memory config.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Shared helpers: unit file
# ---------------------------------------------------------------------------

_UNIT_TMPL = """\
[Unit]
Description=CIDX Multi-User Server
After=network.target

[Service]
Type=simple
User=code-indexer
WorkingDirectory=/opt/code-indexer
ExecStart=/opt/venv/bin/python3 -m code_indexer.server.main {flags}
Restart=always

[Install]
WantedBy=multi-user.target
"""


def _write_unit_file(
    unit_dir: Path,
    host: str = "0.0.0.0",
    port: int = 8000,
    workers: int = 4,
) -> None:
    """Write a cidx-server.service with the given flags to unit_dir."""
    unit_dir.mkdir(parents=True, exist_ok=True)
    flags = f"--host {host} --port {port} --workers {workers}"
    (unit_dir / "cidx-server.service").write_text(_UNIT_TMPL.format(flags=flags))


def _make_svc(server_dir: Path, config_json: dict):  # type: ignore[no-untyped-def]
    """Build a ConfigService backed by a real config.json."""
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfigManager

    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "config.json").write_text(json.dumps(config_json))
    mgr = ServerConfigManager(server_dir_path=str(server_dir))
    return ConfigService(config_manager=mgr)


def _init_sqlite_db(svc, db_path: Path, unit_dir: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Initialize schema + first-boot SQLite seed with the fake unit dir active."""
    import code_indexer.server.auto_update.deployment_executor as de_mod
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(str(db_path)).initialize_database()
    monkeypatch.setattr(de_mod, "SYSTEMD_UNIT_DIR", unit_dir)
    svc.initialize_runtime_db(str(db_path))


def _read_seeded_runtime_row(db_path: Path) -> dict:
    """Read the 'runtime' row from the SQLite DB and return config_json as dict."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT config_json FROM server_config WHERE config_key = 'runtime'"
        ).fetchone()
        assert row is not None, "No 'runtime' row found after initialize_runtime_db"
        return json.loads(row[0])  # type: ignore[no-any-return]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _FakePool / _FakeCursor for the PG seed-path test
# ---------------------------------------------------------------------------
# Mirrors the faithful psycopg3 fake in test_materialize_launch_config_1198.py.
# Used to exercise _seed_runtime_to_pg (which uses psycopg3-specific APIs) via
# a real SQLite backend so we can inspect what was actually inserted.


class _DictRow(dict):
    """dict-like row (mirrors psycopg dict_row output)."""


class _FakeCursor:
    """Faithful psycopg3 cursor backed by real sqlite3 cursor.

    Translates PG SQL (%s placeholders, EXCLUDED.) to SQLite equivalents.
    When row_factory=dict_row the results are _DictRow objects.
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

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args) -> None:
        self._cur.close()


class _FakeConnection:
    """Faithful psycopg3 connection over a real sqlite3.Connection (borrow semantics)."""

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
        pass  # pool retains connection ownership


class _FakePool:
    """Faithful psycopg3 ConnectionPool over a real sqlite3.Connection."""

    def __init__(self, sqlite_conn) -> None:  # type: ignore[no-untyped-def]
        _conn = sqlite_conn

        @contextmanager  # type: ignore[misc]
        def _connection():  # type: ignore[no-untyped-def]
            yield _FakeConnection(_conn)

        self.connection = _connection


def _create_server_config_table(conn) -> None:  # type: ignore[no-untyped-def]
    """Create server_config table in a sqlite3 connection (mirrors real schema)."""
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


# ---------------------------------------------------------------------------
# Test 1: host absent from config.json + ExecStart 0.0.0.0 → seeded host=0.0.0.0
# ---------------------------------------------------------------------------


class TestSeedLayerHostGapFill:
    """First-boot: 'host' absent from config.json → ExecStart value fills the gap."""

    def test_host_gap_filled_in_memory_at_seed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Bug #1232: config.json no 'host'; ExecStart 0.0.0.0 → in-memory host=0.0.0.0."""
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="0.0.0.0", workers=4)

        svc = _make_svc(
            tmp_path / "server",
            {"port": 8000, "workers": 4, "log_level": "INFO"},  # no 'host'
        )
        _init_sqlite_db(svc, tmp_path / "cidx.db", unit_dir, monkeypatch)

        assert svc.get_config().host == "0.0.0.0", (
            f"Bug #1232: in-memory host must be 0.0.0.0 from ExecStart, "
            f"got {svc.get_config().host!r}"
        )

    def test_host_gap_filled_in_sqlite_db_row(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The SQLite DB row (not just in-memory config) carries the seeded host=0.0.0.0.

        Verifies that _extract_runtime_dict saw the corrected config after backfill
        and _save_runtime_to_sqlite persisted it correctly.
        """
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="0.0.0.0", workers=4)

        svc = _make_svc(
            tmp_path / "server",
            {"port": 8000, "workers": 4, "log_level": "INFO"},  # no 'host'
        )
        db_path = tmp_path / "cidx.db"
        _init_sqlite_db(svc, db_path, unit_dir, monkeypatch)

        row = _read_seeded_runtime_row(db_path)
        assert row.get("host") == "0.0.0.0", (
            f"Bug #1232: SQLite DB row must carry seeded host=0.0.0.0, "
            f"got {row.get('host')!r}. The backfill must affect _extract_runtime_dict."
        )

    def test_stripped_config_json_no_longer_carries_host(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Story #1196: after cleanup, stripped config.json does NOT carry host.

        Bug #1232's backfill still gap-fills the in-memory config and the SQLite
        DB row (see the other two tests in this class) -- that mechanism is
        untouched by Story #1196. What changes is the config.json write/strip
        path: since TRANSITION_PRESERVE_KEYS is removed, 'host' (like the other
        three launch keys) no longer survives the strip.
        """
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="0.0.0.0", workers=4)

        svc = _make_svc(
            tmp_path / "server",
            {"port": 8000, "workers": 4, "log_level": "INFO"},  # no 'host'
        )
        _init_sqlite_db(svc, tmp_path / "cidx.db", unit_dir, monkeypatch)

        config_file = Path(svc.config_manager.config_file_path)
        stored = json.loads(config_file.read_text())
        assert "host" not in stored, (
            f"Story #1196: stripped config.json must NOT carry 'host' anymore "
            f"(transition allow-list removed). Got: {stored.get('host')!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: config.json WITH explicit host → preserved (ExecStart cannot override)
# ---------------------------------------------------------------------------


class TestSeedLayerExplicitConfigJsonWins:
    """Explicit config.json values are respected — ExecStart only fills GAPS."""

    def test_explicit_host_in_config_json_preserved(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """config.json has explicit host=0.0.0.0; ExecStart=192.168.1.50 → 0.0.0.0 wins."""
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="192.168.1.50")  # different from config.json

        svc = _make_svc(
            tmp_path / "server",
            {"host": "0.0.0.0", "port": 8000, "workers": 4, "log_level": "INFO"},
        )
        db_path = tmp_path / "cidx.db"
        _init_sqlite_db(svc, db_path, unit_dir, monkeypatch)

        # Both in-memory and DB row must show explicit config.json value
        assert svc.get_config().host == "0.0.0.0", (
            f"Explicit config.json host=0.0.0.0 must not be overwritten by ExecStart. "
            f"Got: {svc.get_config().host!r}"
        )
        row = _read_seeded_runtime_row(db_path)
        assert row.get("host") == "0.0.0.0", (
            f"Seeded DB row must carry explicit config.json host=0.0.0.0. "
            f"Got: {row.get('host')!r}"
        )

    def test_explicit_workers_in_config_json_preserved(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """config.json has workers=4; ExecStart has workers=8 → 4 wins."""
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="0.0.0.0", workers=8)  # different

        svc = _make_svc(
            tmp_path / "server",
            {"host": "0.0.0.0", "port": 8000, "workers": 4, "log_level": "INFO"},
        )
        db_path = tmp_path / "cidx.db"
        _init_sqlite_db(svc, db_path, unit_dir, monkeypatch)

        assert svc.get_config().workers == 4, (
            f"Explicit config.json workers=4 must be preserved over ExecStart 8. "
            f"Got: {svc.get_config().workers!r}"
        )
        row = _read_seeded_runtime_row(db_path)
        assert row.get("workers") == 4, (
            f"Seeded DB row must carry explicit config.json workers=4. "
            f"Got: {row.get('workers')!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: no ExecStart available + host absent → ServerConfig default + WARNING
# ---------------------------------------------------------------------------


class TestSeedLayerFallbackWithWarning:
    """No ExecStart + host absent from config.json → 127.0.0.1 (default) + WARNING."""

    def test_fallback_to_default_host_with_warning(
        self, tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No service file + no 'host' in config.json → host=127.0.0.1 + WARNING."""
        unit_dir = tmp_path / "no-systemd"
        unit_dir.mkdir()  # empty — no service file

        svc = _make_svc(
            tmp_path / "server",
            {"port": 8000, "workers": 1, "log_level": "INFO"},  # no 'host'
        )

        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.services.config_service",
        ):
            _init_sqlite_db(svc, tmp_path / "cidx.db", unit_dir, monkeypatch)

        assert svc.get_config().host == "127.0.0.1", (
            f"Fallback: default host 127.0.0.1 expected when ExecStart unavailable "
            f"and host absent from config.json. Got: {svc.get_config().host!r}"
        )
        warning_msgs = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "host" in m.lower() or "default" in m.lower() or "absent" in m.lower()
            for m in warning_msgs
        ), (
            f"A WARNING must be logged when falling back to the default host. "
            f"Logged warnings: {warning_msgs!r}"
        )


# ---------------------------------------------------------------------------
# Test 4: port and workers gap-filled from ExecStart analogously
# ---------------------------------------------------------------------------


class TestSeedLayerPortAndWorkers:
    """port and workers are also gap-filled from ExecStart at seed."""

    def test_workers_gap_filled_from_execstart(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """ExecStart --workers 8; config.json has no 'workers' → seeded workers=8."""
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="0.0.0.0", port=8000, workers=8)

        svc = _make_svc(
            tmp_path / "server",
            {"host": "0.0.0.0", "port": 8000, "log_level": "INFO"},  # no 'workers'
        )
        db_path = tmp_path / "cidx.db"
        _init_sqlite_db(svc, db_path, unit_dir, monkeypatch)

        assert svc.get_config().workers == 8, (
            f"Bug #1232: workers must be 8 from ExecStart. Got: {svc.get_config().workers!r}"
        )
        row = _read_seeded_runtime_row(db_path)
        assert row.get("workers") == 8, (
            f"Bug #1232: seeded DB row must carry workers=8. Got: {row.get('workers')!r}"
        )

    def test_port_gap_filled_from_execstart(self, tmp_path: Path, monkeypatch) -> None:
        """ExecStart --port 9000; config.json has no 'port' → seeded port=9000."""
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="0.0.0.0", port=9000, workers=2)

        svc = _make_svc(
            tmp_path / "server",
            {"host": "0.0.0.0", "workers": 2, "log_level": "INFO"},  # no 'port'
        )
        db_path = tmp_path / "cidx.db"
        _init_sqlite_db(svc, db_path, unit_dir, monkeypatch)

        assert svc.get_config().port == 9000, (
            f"Bug #1232: port must be 9000 from ExecStart. Got: {svc.get_config().port!r}"
        )
        row = _read_seeded_runtime_row(db_path)
        assert row.get("port") == 9000, (
            f"Bug #1232: seeded DB row must carry port=9000. Got: {row.get('port')!r}"
        )

    def test_all_three_gap_filled_when_all_absent_from_config_json(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """config.json has only log_level; host/port/workers all come from ExecStart."""
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="192.168.1.100", port=7500, workers=6)

        svc = _make_svc(
            tmp_path / "server",
            {"log_level": "INFO"},  # no host, port, or workers
        )
        db_path = tmp_path / "cidx.db"
        _init_sqlite_db(svc, db_path, unit_dir, monkeypatch)

        cfg = svc.get_config()
        assert cfg.host == "192.168.1.100", f"Got host={cfg.host!r}"
        assert cfg.port == 7500, f"Got port={cfg.port!r}"
        assert cfg.workers == 6, f"Got workers={cfg.workers!r}"

        row = _read_seeded_runtime_row(db_path)
        assert row.get("host") == "192.168.1.100", f"DB row host={row.get('host')!r}"
        assert row.get("port") == 7500, f"DB row port={row.get('port')!r}"
        assert row.get("workers") == 6, f"DB row workers={row.get('workers')!r}"


# ---------------------------------------------------------------------------
# Test PG path: _seed_runtime_to_pg also gap-fills from ExecStart
# ---------------------------------------------------------------------------


class TestPgSeedLayerGapFill:
    """_seed_runtime_to_pg (cluster first-boot path) also backfills from ExecStart.

    Uses _FakePool/_FakeConnection backed by a real in-memory SQLite DB to capture
    what was inserted, then reads it back to verify the gap-filled values appear
    in the seeded runtime row.
    """

    def test_pg_seed_host_gap_filled_from_execstart(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PG first-boot: config.json no 'host'; ExecStart 0.0.0.0 → seeded row host=0.0.0.0."""
        import code_indexer.server.auto_update.deployment_executor as de_mod

        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="0.0.0.0", port=8000, workers=4)

        svc = _make_svc(
            tmp_path / "server",
            {"port": 8000, "workers": 4, "log_level": "INFO"},  # no 'host'
        )
        svc.load_config()  # populate self._config from file

        # Build in-memory SQLite DB as the PG stand-in
        pg_db = sqlite3.connect(":memory:")
        try:
            _create_server_config_table(pg_db)
            svc._pool = _FakePool(pg_db)

            monkeypatch.setattr(de_mod, "SYSTEMD_UNIT_DIR", unit_dir)
            svc._seed_runtime_to_pg()

            # Read back the seeded row
            row = pg_db.execute(
                "SELECT config_json FROM server_config WHERE config_key = 'runtime'"
            ).fetchone()
            assert row is not None, "No 'runtime' row after _seed_runtime_to_pg"
            seeded = json.loads(row[0])
        finally:
            pg_db.close()

        assert seeded.get("host") == "0.0.0.0", (
            f"Bug #1232 PG path: seeded row host must be 0.0.0.0 from ExecStart, "
            f"got {seeded.get('host')!r}"
        )

    def test_pg_seed_explicit_host_preserved(self, tmp_path: Path, monkeypatch) -> None:
        """PG first-boot: config.json has explicit host=0.0.0.0 → preserved in seeded row."""
        import code_indexer.server.auto_update.deployment_executor as de_mod

        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="192.168.1.50")  # different from config

        svc = _make_svc(
            tmp_path / "server",
            {"host": "0.0.0.0", "port": 8000, "workers": 4, "log_level": "INFO"},
        )
        svc.load_config()

        pg_db = sqlite3.connect(":memory:")
        try:
            _create_server_config_table(pg_db)
            svc._pool = _FakePool(pg_db)

            monkeypatch.setattr(de_mod, "SYSTEMD_UNIT_DIR", unit_dir)
            svc._seed_runtime_to_pg()

            row = pg_db.execute(
                "SELECT config_json FROM server_config WHERE config_key = 'runtime'"
            ).fetchone()
            assert row is not None
            seeded = json.loads(row[0])
        finally:
            pg_db.close()

        assert seeded.get("host") == "0.0.0.0", (
            f"PG seed: explicit config.json host=0.0.0.0 must be preserved. "
            f"Got: {seeded.get('host')!r}"
        )


# ---------------------------------------------------------------------------
# Test 5: After seed, admin workers change → materialize honors desired state
# ---------------------------------------------------------------------------


class TestMaterializeHonorsDesiredStateAfterSeed:
    """PART A verification: materialize_launch_config uses desired state, not ExecStart.

    After first-boot seed (where backfill may have gap-filled from ExecStart),
    an admin change via save_config() must be reflected in the next materialize
    output — not the stale live ExecStart value.
    """

    def test_admin_workers_change_honored_by_materialize(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Admin sets workers=6 → launch.json workers=6 (not ExecStart workers=1).

        ExecStart has workers=1 (stale). config.json has explicit workers=2 (preserved
        through seed). After admin changes to workers=6, materialize must write 6,
        proving it uses desired state (self._config), never the live ExecStart.
        """
        import code_indexer.server.services.config_service as cs_mod
        from dataclasses import replace

        # ExecStart advertises workers=1 (the stale live value)
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="0.0.0.0", port=8000, workers=1)

        launch_path = tmp_path / "launch.json"
        monkeypatch.setattr(cs_mod, "LAUNCH_CONFIG_PATH", launch_path)

        # config.json has explicit workers=2 (preserved, not gap-filled)
        svc = _make_svc(
            tmp_path / "server",
            {"host": "0.0.0.0", "port": 8000, "workers": 2, "log_level": "INFO"},
        )
        _init_sqlite_db(svc, tmp_path / "cidx.db", unit_dir, monkeypatch)

        # Admin changes workers to 6 via the Web UI
        current = svc.get_config()
        svc.save_config(replace(current, workers=6))

        assert launch_path.exists(), "launch.json must exist after save_config()"
        data = json.loads(launch_path.read_text())
        assert data["workers"] == 6, (
            f"Materialize must honor admin-desired workers=6, not stale ExecStart "
            f"workers=1. Got: {data['workers']!r}. "
            "This proves materialize_launch_config uses desired state (Bug #1232 rework)."
        )
