"""Story #1200 AC1 + AC2: bump_launch_restart_generation() and save-after-bump.

RED -> GREEN -> REFACTOR.

AC1  -- bump_launch_restart_generation(): atomic increment, version++, no asdict.
MAJOR-M3 -- bump must NOT advance _db_config_version.
AC2  -- save-after-bump preserves launch_restart_generation; no dropped-key resurrection.
Defect 2+3 -- PG paths verified via SQL-text assertion tests (mock pool captures SQL).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers: real SQLite runtime row
# ---------------------------------------------------------------------------

_INITIAL_VERSION = 1


def _make_sqlite_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config ("
            "config_key TEXT PRIMARY KEY DEFAULT 'runtime', "
            "config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT DEFAULT (datetime('now')), "
            "updated_by TEXT)"
        )
        conn.commit()


def _seed_runtime_row(
    db_path: str, data: dict, version: int = _INITIAL_VERSION
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO server_config (config_key, config_json, version, updated_by) "
            "VALUES ('runtime', ?, ?, 'test') "
            "ON CONFLICT(config_key) DO UPDATE SET "
            "config_json = excluded.config_json, "
            "version = excluded.version",
            (json.dumps(data), version),
        )
        conn.commit()


def _read_runtime_row(db_path: str) -> dict:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT config_json, version FROM server_config WHERE config_key = 'runtime'"
        ).fetchone()
    assert row is not None
    return {"data": json.loads(row[0]), "version": row[1]}


def _make_config_service(tmp_path: Path, db_path: str):
    from code_indexer.server.services.config_service import ConfigService

    svc = ConfigService(server_dir_path=str(tmp_path))
    svc.load_config()
    svc._sqlite_db_path = db_path
    return svc


# ---------------------------------------------------------------------------
# Source guard: method must exist in config_service.py
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONFIG_SERVICE_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "services" / "config_service.py"
)


class TestBumpMethodExists:
    """Source guard: bump_launch_restart_generation must be defined."""

    def test_method_defined_in_config_service(self) -> None:
        source = _CONFIG_SERVICE_PATH.read_text()
        assert "def bump_launch_restart_generation" in source, (
            "AC1: ConfigService must define bump_launch_restart_generation()"
        )


# ===========================================================================
# AC1: atomic bump increments generation + version (real SQLite)
# ===========================================================================


@pytest.mark.slow
class TestBumpLaunchRestartGenerationSQLite:
    """AC1 behavioral: real SQLite, single-row atomic bump."""

    def test_bump_increments_generation_from_zero(self, tmp_path: Path) -> None:
        """First bump: absent generation (COALESCE 0) -> 1; version++."""
        db_path = str(tmp_path / "cidx.db")
        _make_sqlite_db(db_path)
        _seed_runtime_row(db_path, {"workers": 2, "log_level": "INFO"})

        svc = _make_config_service(tmp_path, db_path)
        svc.bump_launch_restart_generation()

        row = _read_runtime_row(db_path)
        assert row["data"].get("launch_restart_generation") == 1, (
            "AC1: first bump must set launch_restart_generation to 1 (COALESCE 0 + 1)"
        )
        assert row["version"] == _INITIAL_VERSION + 1, (
            "AC1: bump must increment version"
        )

    def test_bump_increments_existing_generation(self, tmp_path: Path) -> None:
        """Bump when generation already > 0 increments correctly."""
        db_path = str(tmp_path / "cidx.db")
        _make_sqlite_db(db_path)
        _seed_runtime_row(
            db_path,
            {"workers": 2, "launch_restart_generation": 5},
            version=3,
        )

        svc = _make_config_service(tmp_path, db_path)
        svc.bump_launch_restart_generation()

        row = _read_runtime_row(db_path)
        assert row["data"].get("launch_restart_generation") == 6, (
            "AC1: bump must increment existing generation (5 -> 6)"
        )
        assert row["version"] == 4, "AC1: version must be bumped alongside generation"

    def test_bump_does_not_advance_db_config_version(self, tmp_path: Path) -> None:
        """MAJOR-M3: bump MUST NOT update _db_config_version on the bumping node.

        This ensures the bumping node's next poll sees the new version and
        triggers its own self-signal via check_pending_launch_restart().
        """
        db_path = str(tmp_path / "cidx.db")
        _make_sqlite_db(db_path)
        _seed_runtime_row(db_path, {"workers": 1}, version=5)

        svc = _make_config_service(tmp_path, db_path)
        svc._db_config_version = 5  # simulate node's tracked version
        svc.bump_launch_restart_generation()

        assert svc._db_config_version == 5, (
            "MAJOR-M3: bump must NOT advance _db_config_version so the bumping "
            "node's next poll detects the new version and self-signals"
        )

    def test_bump_does_not_use_asdict_round_trip(self, tmp_path: Path) -> None:
        """AC1: bump must NOT go through the asdict() read-modify-write path.

        Guard: _extract_runtime_dict must NOT be called during bump.
        """
        from unittest.mock import patch

        db_path = str(tmp_path / "cidx.db")
        _make_sqlite_db(db_path)
        _seed_runtime_row(db_path, {"workers": 1})

        svc = _make_config_service(tmp_path, db_path)
        with patch.object(
            type(svc), "_extract_runtime_dict", wraps=svc._extract_runtime_dict
        ) as mock_extract:
            svc.bump_launch_restart_generation()
            assert mock_extract.call_count == 0, (
                "AC1: bump_launch_restart_generation must NOT call "
                "_extract_runtime_dict (no asdict round-trip)"
            )


# ===========================================================================
# AC2: save-after-bump preserves launch_restart_generation, no dropped keys
# ===========================================================================


@pytest.mark.slow
class TestSavePreservesGenerationSQLite:
    """AC2 behavioral: real SQLite, save preserves existing raw generation."""

    def test_save_after_bump_preserves_generation(self, tmp_path: Path) -> None:
        """Save config after bump must retain launch_restart_generation in row."""
        db_path = str(tmp_path / "cidx.db")
        _make_sqlite_db(db_path)
        _seed_runtime_row(
            db_path,
            {"workers": 2, "log_level": "INFO", "launch_restart_generation": 3},
            version=5,
        )

        svc = _make_config_service(tmp_path, db_path)
        svc._load_runtime_from_sqlite()
        config = svc.get_config()
        svc.save_config(config)

        row = _read_runtime_row(db_path)
        assert row["data"].get("launch_restart_generation") == 3, (
            "AC2: save_config must preserve launch_restart_generation from the "
            "current row; wholesale overwrite would delete it"
        )

    def test_save_does_not_resurrect_orphan_keys(self, tmp_path: Path) -> None:
        """AC2: save must NOT bring back keys intentionally excluded from runtime dict.

        We plant an extra 'orphan_dropped_key' in the DB row directly, then save
        through the normal path.  The orphan must NOT reappear after save, while
        launch_restart_generation MUST be preserved via targeted re-inject.
        """
        db_path = str(tmp_path / "cidx.db")
        _make_sqlite_db(db_path)
        _seed_runtime_row(
            db_path,
            {
                "workers": 2,
                "launch_restart_generation": 7,
                "orphan_dropped_key": "should_vanish",
            },
        )

        svc = _make_config_service(tmp_path, db_path)
        svc._load_runtime_from_sqlite()
        config = svc.get_config()
        svc.save_config(config)

        row = _read_runtime_row(db_path)
        assert "orphan_dropped_key" not in row["data"], (
            "AC2: save must NOT resurrect keys dropped by _extract_runtime_dict; "
            "only launch_restart_generation must be preserved via targeted re-inject"
        )
        assert row["data"].get("launch_restart_generation") == 7, (
            "AC2: save must preserve the generation via targeted re-inject"
        )

    def test_save_reads_current_row_generation_not_stale_snapshot(
        self, tmp_path: Path
    ) -> None:
        """AC2: save reads the CURRENT row's generation from DB (sequential).

        Sequential proof: bump the row directly after svc loads config, then
        call save.  The save must pick up the post-bump value from the DB row,
        not the stale in-memory value from when config was loaded.

        NOTE: SQLite uses BEGIN IMMEDIATE which serialises the transaction, so
        this proves the row-read-at-save-time semantics.  The PG atomic-UPDATE
        form is proved separately by test_save_pg_uses_atomic_jsonb_set_update.
        """
        db_path = str(tmp_path / "cidx.db")
        _make_sqlite_db(db_path)
        _seed_runtime_row(db_path, {"workers": 2, "launch_restart_generation": 0})

        svc = _make_config_service(tmp_path, db_path)
        svc._load_runtime_from_sqlite()

        # Simulate concurrent bump by another process
        with sqlite3.connect(db_path) as conn:
            raw = conn.execute(
                "SELECT config_json FROM server_config WHERE config_key='runtime'"
            ).fetchone()[0]
            data = json.loads(raw)
            data["launch_restart_generation"] = 1
            conn.execute(
                "UPDATE server_config SET config_json=?, version=version+1 "
                "WHERE config_key='runtime'",
                (json.dumps(data),),
            )
            conn.commit()

        # svc saves its config (stale in-memory generation=0)
        config = svc.get_config()
        svc.save_config(config)

        row = _read_runtime_row(db_path)
        assert row["data"].get("launch_restart_generation") == 1, (
            "AC2: save must read the CURRENT row's generation from the DB row "
            "(not the stale in-memory snapshot)"
        )


# ===========================================================================
# Defect 2+3: PG SQL-text assertion tests (mock pool — validates SQL shape)
# ===========================================================================


def _make_pg_pool(fetchone_side_effect=None):
    """Build a MagicMock pool that mimics psycopg3 connection/cursor context managers.

    Returns (pool, conn, cur) so callers can inspect cur.execute.call_args_list.

    The cursor is returned by conn.cursor(row_factory=...) used as a context manager.
    fetchone_side_effect: if provided, a list of return values consumed in order
    by successive fetchone() calls (for multi-fetchone methods like _save_runtime_to_pg).
    """
    cur = MagicMock()
    if fetchone_side_effect is not None:
        cur.execute.return_value.fetchone.side_effect = fetchone_side_effect
    else:
        # After the fix there is no pre-SELECT: only the post-UPDATE version
        # read-back fetchone() is issued.
        cur.execute.return_value.fetchone.side_effect = [
            {"version": 7},
        ]

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return pool, conn, cur


def _make_pg_config_service(tmp_path: Path, pool: MagicMock):
    """Build a ConfigService wired with a mock PG pool (no SQLite path)."""
    from code_indexer.server.services.config_service import ConfigService

    svc = ConfigService(server_dir_path=str(tmp_path))
    svc.load_config()
    svc._pool = pool
    svc._sqlite_db_path = None
    return svc


class TestSaveRuntimeToPgSqlText:
    """Defect 2: _save_runtime_to_pg must use a single atomic jsonb_set UPDATE.

    The racy SELECT-then-UPDATE pattern is detected by asserting the UPDATE SQL
    contains jsonb_set and '{launch_restart_generation}' — proving the generation
    is preserved INSIDE the UPDATE itself rather than patched in Python after a
    separate SELECT.
    """

    def test_save_pg_uses_atomic_jsonb_set_update(self, tmp_path: Path) -> None:
        """RED: current racy code does a plain UPDATE without jsonb_set.

        This test MUST FAIL against the current code (plain UPDATE) and pass
        after the fix (atomic jsonb_set UPDATE).
        """
        pool, conn, cur = _make_pg_pool()
        svc = _make_pg_config_service(tmp_path, pool)
        config = svc.get_config()

        # Patch materialize_launch_config to avoid filesystem side effects
        svc.materialize_launch_config = MagicMock(return_value=True)  # type: ignore[method-assign]

        svc._save_runtime_to_pg(config)

        # Collect all SQL strings passed to cur.execute
        all_sql_calls = [str(c.args[0]) for c in cur.execute.call_args_list]

        # Find the UPDATE statement
        update_calls = [sql for sql in all_sql_calls if "UPDATE server_config" in sql]
        assert update_calls, (
            "Defect 2: _save_runtime_to_pg must issue an UPDATE server_config statement"
        )

        # The UPDATE must use jsonb_set to atomically preserve launch_restart_generation
        update_sql = update_calls[0]
        assert "jsonb_set" in update_sql, (
            "Defect 2: the UPDATE must use jsonb_set() to atomically preserve "
            "launch_restart_generation inside the same statement — "
            "a plain UPDATE without jsonb_set is the racy SELECT+overwrite pattern"
        )
        assert "'{launch_restart_generation}'" in update_sql or (
            "launch_restart_generation" in update_sql
        ), "Defect 2: jsonb_set must target the '{launch_restart_generation}' key"

    def test_save_pg_update_includes_version_bump(self, tmp_path: Path) -> None:
        """The atomic UPDATE must also increment version in the same statement."""
        pool, conn, cur = _make_pg_pool()
        svc = _make_pg_config_service(tmp_path, pool)
        config = svc.get_config()
        svc.materialize_launch_config = MagicMock(return_value=True)  # type: ignore[method-assign]

        svc._save_runtime_to_pg(config)

        all_sql_calls = [str(c.args[0]) for c in cur.execute.call_args_list]
        update_calls = [sql for sql in all_sql_calls if "UPDATE server_config" in sql]
        assert update_calls, "Must issue UPDATE server_config"

        update_sql = update_calls[0]
        assert "version" in update_sql and (
            "version + 1" in update_sql or "version+1" in update_sql
        ), (
            "Defect 2: the atomic UPDATE must include version = version + 1 "
            "in the same statement"
        )

    def test_save_pg_no_separate_pre_select_for_generation(
        self, tmp_path: Path
    ) -> None:
        """The racy pre-SELECT (SELECT config_json ... for Python patch) must be gone.

        After the fix there should be NO SELECT before the UPDATE that reads
        config_json for the purpose of patching launch_restart_generation in Python.
        The only SELECT allowed is the post-UPDATE version read-back.
        """
        pool, conn, cur = _make_pg_pool(
            fetchone_side_effect=[
                {"version": 7},  # only one fetchone: the post-UPDATE version read-back
            ]
        )
        svc = _make_pg_config_service(tmp_path, pool)
        config = svc.get_config()
        svc.materialize_launch_config = MagicMock(return_value=True)  # type: ignore[method-assign]

        svc._save_runtime_to_pg(config)

        all_sql_calls = [str(c.args[0]) for c in cur.execute.call_args_list]
        select_calls = [
            sql for sql in all_sql_calls if sql.strip().upper().startswith("SELECT")
        ]
        # After fix: only the post-UPDATE version read-back SELECT is allowed
        assert len(select_calls) <= 1, (
            "Defect 2: after the fix there must be at most one SELECT (the version "
            "read-back); a pre-UPDATE SELECT config_json is the racy pattern that "
            "enables lost-update races"
        )


class TestBumpLaunchRestartGenerationPgSqlText:
    """Defect 3: bump_launch_restart_generation PG path SQL-text assertions."""

    def test_bump_pg_uses_jsonb_set_in_single_statement(self, tmp_path: Path) -> None:
        """PG bump must use jsonb_set in a single UPDATE (no read-modify-write)."""
        pool, conn, cur = _make_pg_pool(fetchone_side_effect=[])
        svc = _make_pg_config_service(tmp_path, pool)

        svc.bump_launch_restart_generation()

        # conn.execute is used directly (not cur) for the bump
        # Check both conn.execute and cur.execute call args
        conn_exec_calls = [str(c.args[0]) for c in conn.execute.call_args_list]
        cur_exec_calls = [str(c.args[0]) for c in cur.execute.call_args_list]
        all_exec_calls = conn_exec_calls + cur_exec_calls

        update_calls = [sql for sql in all_exec_calls if "UPDATE server_config" in sql]
        assert update_calls, (
            "Defect 3: bump_launch_restart_generation PG path must issue "
            "UPDATE server_config"
        )

        update_sql = update_calls[0]
        assert "jsonb_set" in update_sql, (
            "Defect 3: PG bump must use jsonb_set() for atomic in-DB increment"
        )
        assert "version" in update_sql and (
            "version + 1" in update_sql or "version+1" in update_sql
        ), "Defect 3: PG bump must increment version in the same statement"
