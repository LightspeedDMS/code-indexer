"""
TDD regression tests for Bug #1335: ConfigService first-boot PG seed gap-fills
host/port/workers from an UNRELATED systemd unit because
_backfill_launch_keys_from_execstart re-reads a config.json that a PRIOR
bootstrap step (_strip_config_file_to_bootstrap, invoked from
initialize_runtime_db) has already stripped of those keys.

Root cause (see GitHub issue #1335):

1. service_init.py calls config_service.initialize_runtime_db(db_path)
   UNCONDITIONALLY on every node (solo AND cluster) BEFORE the PG pool is
   wired up.  On first boot this reads config.json (still has the operator's
   explicit host/port/workers), correctly backfills any gaps from ExecStart,
   saves the runtime dict to the node-local SQLite server_config row, and
   THEN STRIPS config.json down to bootstrap-only keys
   (_strip_config_file_to_bootstrap) -- host/port/workers no longer exist on
   disk after this point.

2. Later in lifespan.py, set_connection_pool() wires the PG pool.  On a truly
   fresh cluster (no PG server_config row yet), _load_runtime_from_pg() calls
   _seed_runtime_to_pg(), which calls _backfill_launch_keys_from_execstart()
   AGAIN.  That function re-reads config.json from disk to answer "was this
   an explicit operator value?" -- but config.json was ALREADY stripped in
   step 1, so every key looks "absent," and the function incorrectly
   gap-fills from read_execstart_flags() -- i.e. from whatever UNRELATED
   cidx-server.service systemd unit happens to be installed on the host.

This file reproduces that exact two-step sequence (initialize_runtime_db
followed by _seed_runtime_to_pg on the SAME ConfigService instance, mirroring
the real single-process startup order) and asserts the bootstrap-explicit
values survive.

Uses the same faithful-fake conventions already established in
test_config_service_execstart_seeding_1232.py for this module: real
ConfigService + real config.json + real SQLite (via DatabaseSchema +
initialize_runtime_db), and a _FakePool/_FakeConnection/_FakeCursor backed by
a real SQLite connection (translating psycopg3-style %s/dict_row semantics)
standing in for the PG pool exercised by _seed_runtime_to_pg. A fake systemd
unit file on disk (monkeypatched SYSTEMD_UNIT_DIR) supplies the
"differently-flagged unrelated unit."
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_config_service_execstart_seeding_1232.py)
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
    """Write a cidx-server.service with the given flags to unit_dir.

    Represents an UNRELATED cidx-server.service that happens to be installed
    on the same host as the freshly-provisioned PG/cluster node -- exactly
    the #1324 repro scenario.
    """
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
    """Run the REAL first-boot SQLite seed (step 1 of the bug sequence).

    This is what strips config.json down to bootstrap-only keys -- the exact
    precondition #1335 requires before _seed_runtime_to_pg() is invoked.
    """
    import code_indexer.server.auto_update.deployment_executor as de_mod
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(str(db_path)).initialize_database()
    monkeypatch.setattr(de_mod, "SYSTEMD_UNIT_DIR", unit_dir)
    svc.initialize_runtime_db(str(db_path))


# ---------------------------------------------------------------------------
# _FakePool / _FakeConnection / _FakeCursor for the PG seed-path test
# (identical faithful-fake pattern used by test_config_service_execstart_seeding_1232.py
# and test_materialize_launch_config_1198.py for this module.)
# ---------------------------------------------------------------------------


class _DictRow(dict):
    """dict-like row (mirrors psycopg dict_row output)."""


class _FakeCursor:
    """Faithful psycopg3 cursor backed by real sqlite3 cursor."""

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


def _seed_to_pg_and_read_back(svc, unit_dir: Path, monkeypatch) -> dict:  # type: ignore[no-untyped-def]
    """Run the REAL step 2 of the bug sequence (_seed_runtime_to_pg) and
    return the seeded runtime row as a dict, reading it back from the fake
    PG-backed SQLite connection -- never inspecting in-memory state only.
    """
    import code_indexer.server.auto_update.deployment_executor as de_mod

    pg_db = sqlite3.connect(":memory:")
    try:
        _create_server_config_table(pg_db)
        svc._pool = _FakePool(pg_db)
        monkeypatch.setattr(de_mod, "SYSTEMD_UNIT_DIR", unit_dir)
        svc._seed_runtime_to_pg()

        row = pg_db.execute(
            "SELECT config_json FROM server_config WHERE config_key = 'runtime'"
        ).fetchone()
        assert row is not None, "No 'runtime' row after _seed_runtime_to_pg"
        return json.loads(row[0])  # type: ignore[no-any-return]
    finally:
        pg_db.close()


# ---------------------------------------------------------------------------
# Bug #1335 regression tests
# ---------------------------------------------------------------------------


class TestBug1335BootstrapIntentSnapshot:
    """First-boot cluster sequence: SQLite strip (step 1) THEN PG seed (step 2)
    on the SAME ConfigService instance must still honor the bootstrap-explicit
    host/port/workers -- not the unrelated systemd unit's ExecStart flags.
    """

    def test_pg_seed_preserves_bootstrap_host_after_sqlite_strip(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """#1335 repro: config.json explicit host=127.0.0.1; an UNRELATED
        cidx-server.service on the same host is bound to 0.0.0.0.  After
        initialize_runtime_db() strips config.json (step 1) and
        _seed_runtime_to_pg() runs (step 2), the seeded PG row must still
        carry host=127.0.0.1 -- the bootstrap intent -- NOT 0.0.0.0.
        """
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="0.0.0.0", port=8000, workers=4)

        svc = _make_svc(
            tmp_path / "server",
            {"host": "127.0.0.1", "port": 8000, "workers": 4, "log_level": "INFO"},
        )
        # Step 1: real first-boot SQLite seed -- strips config.json.
        _init_sqlite_db(svc, tmp_path / "cidx.db", unit_dir, monkeypatch)
        config_file = Path(svc.config_manager.config_file_path)
        stripped = json.loads(config_file.read_text())
        assert "host" not in stripped, (
            "Precondition failed: config.json must already be stripped of "
            "'host' before step 2 runs (that is the exact bug precondition)."
        )

        # Step 2: PG first-boot seed, with the unrelated 0.0.0.0 unit still present.
        seeded = _seed_to_pg_and_read_back(svc, unit_dir, monkeypatch)

        assert seeded.get("host") == "127.0.0.1", (
            "Bug #1335: seeded PG runtime row must preserve the bootstrap "
            "host=127.0.0.1 even though config.json was already stripped and "
            "an unrelated systemd unit advertises host=0.0.0.0. "
            f"Got: {seeded.get('host')!r}"
        )
        assert svc.get_config().host == "127.0.0.1", (
            "Bug #1335: in-memory config must also preserve host=127.0.0.1. "
            f"Got: {svc.get_config().host!r}"
        )

    def test_pg_seed_preserves_bootstrap_port_after_sqlite_strip(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Same scenario for 'port': config.json explicit port=5432 (PG-ish
        port chosen only as a distinctive value); unrelated unit advertises
        port=8000.
        """
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="127.0.0.1", port=8000, workers=4)

        svc = _make_svc(
            tmp_path / "server",
            {"host": "127.0.0.1", "port": 9443, "workers": 4, "log_level": "INFO"},
        )
        _init_sqlite_db(svc, tmp_path / "cidx.db", unit_dir, monkeypatch)
        config_file = Path(svc.config_manager.config_file_path)
        stripped = json.loads(config_file.read_text())
        assert "port" not in stripped, (
            "Precondition failed: config.json must already be stripped of "
            "'port' before step 2 runs."
        )

        seeded = _seed_to_pg_and_read_back(svc, unit_dir, monkeypatch)

        assert seeded.get("port") == 9443, (
            "Bug #1335: seeded PG runtime row must preserve the bootstrap "
            "port=9443 even though config.json was already stripped and an "
            f"unrelated systemd unit advertises port=8000. Got: {seeded.get('port')!r}"
        )
        assert svc.get_config().port == 9443, (
            f"Bug #1335: in-memory config must also preserve port=9443. "
            f"Got: {svc.get_config().port!r}"
        )

    def test_pg_seed_preserves_bootstrap_workers_after_sqlite_strip(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Same scenario for 'workers': config.json explicit workers=2;
        unrelated unit advertises workers=16.
        """
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="127.0.0.1", port=8000, workers=16)

        svc = _make_svc(
            tmp_path / "server",
            {"host": "127.0.0.1", "port": 8000, "workers": 2, "log_level": "INFO"},
        )
        _init_sqlite_db(svc, tmp_path / "cidx.db", unit_dir, monkeypatch)
        config_file = Path(svc.config_manager.config_file_path)
        stripped = json.loads(config_file.read_text())
        assert "workers" not in stripped, (
            "Precondition failed: config.json must already be stripped of "
            "'workers' before step 2 runs."
        )

        seeded = _seed_to_pg_and_read_back(svc, unit_dir, monkeypatch)

        assert seeded.get("workers") == 2, (
            "Bug #1335: seeded PG runtime row must preserve the bootstrap "
            "workers=2 even though config.json was already stripped and an "
            f"unrelated systemd unit advertises workers=16. Got: {seeded.get('workers')!r}"
        )
        assert svc.get_config().workers == 2, (
            f"Bug #1335: in-memory config must also preserve workers=2. "
            f"Got: {svc.get_config().workers!r}"
        )

    def test_pg_seed_preserves_all_three_bootstrap_launch_keys(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Combined scenario: all three (host/port/workers) explicit in
        config.json, all three different on the unrelated systemd unit.
        """
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="0.0.0.0", port=8000, workers=32)

        svc = _make_svc(
            tmp_path / "server",
            {
                "host": "127.0.0.1",
                "port": 9443,
                "workers": 2,
                "log_level": "INFO",
            },
        )
        _init_sqlite_db(svc, tmp_path / "cidx.db", unit_dir, monkeypatch)

        seeded = _seed_to_pg_and_read_back(svc, unit_dir, monkeypatch)

        assert seeded.get("host") == "127.0.0.1", f"Got host={seeded.get('host')!r}"
        assert seeded.get("port") == 9443, f"Got port={seeded.get('port')!r}"
        assert seeded.get("workers") == 2, f"Got workers={seeded.get('workers')!r}"

        cfg = svc.get_config()
        assert cfg.host == "127.0.0.1", f"Got in-memory host={cfg.host!r}"
        assert cfg.port == 9443, f"Got in-memory port={cfg.port!r}"
        assert cfg.workers == 2, f"Got in-memory workers={cfg.workers!r}"

    def test_true_gap_still_backfills_from_execstart_on_pg_seed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Regression guard: a KEY TRULY ABSENT from the original bootstrap
        config.json (not just stripped later) must still be gap-filled from
        ExecStart at PG seed time -- the fix must not turn off legitimate
        gap-filling, only stop it from misreading an already-stripped file.
        """
        unit_dir = tmp_path / "systemd"
        _write_unit_file(unit_dir, host="10.0.0.5", port=8000, workers=4)

        svc = _make_svc(
            tmp_path / "server",
            {"port": 8000, "workers": 4, "log_level": "INFO"},  # no 'host' at all
        )
        _init_sqlite_db(svc, tmp_path / "cidx.db", unit_dir, monkeypatch)

        seeded = _seed_to_pg_and_read_back(svc, unit_dir, monkeypatch)

        assert seeded.get("host") == "10.0.0.5", (
            "A truly-absent bootstrap key must still gap-fill from ExecStart. "
            f"Got: {seeded.get('host')!r}"
        )
