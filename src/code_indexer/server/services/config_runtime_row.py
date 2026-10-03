"""The committed runtime-configuration row (``server_config``, key
``runtime``) on SQLite and PostgreSQL: reads, compare-and-set commits and
insert-only seeding (Bug #2017).

No function here can overwrite a row it did not read: a commit names the
version its change was applied to and writes only while the row is still at
that version; a seed only inserts an absent row.  ``launch_restart_generation``
is not a dataclass field, so every commit carries it over from the row.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

CONFIG_KEY_RUNTIME = "runtime"
UPDATER_WEB_UI = "web-ui"
UPDATER_SEED = "config-seed"
# Bug #1758 convention (DatabaseConnectionManager: busy_timeout 30000): a
# brief lock held by another worker is waited out, not raised after Python's
# 5 s sqlite3.connect() default.
_SQLITE_LOCK_TIMEOUT_SECONDS = 30.0


def _connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path, timeout=_SQLITE_LOCK_TIMEOUT_SECONDS)


def _parsed(raw: Any) -> Dict[str, Any]:
    from code_indexer.server.storage.json_column import parse_json_column

    # JSONB on PostgreSQL (already a dict), TEXT on SQLite
    runtime = parse_json_column(raw, dict, "server_config.config_json")
    if runtime is None:
        raise RuntimeError("committed runtime configuration is not a JSON object")
    return runtime


def read_runtime_pg(pool: Any) -> Optional[Tuple[int, Dict[str, Any]]]:
    """``(version, runtime dict)`` of the committed row; None when absent."""
    from psycopg.rows import dict_row

    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT config_json, version FROM server_config WHERE config_key = %s",
                (CONFIG_KEY_RUNTIME,),
            ).fetchone()
    if row is None:
        return None
    return int(row["version"]), _parsed(row["config_json"])


def read_runtime_version_pg(pool: Any) -> Optional[int]:
    """The committed row's version only; None when absent."""
    from psycopg.rows import dict_row

    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT version FROM server_config WHERE config_key = %s",
                (CONFIG_KEY_RUNTIME,),
            ).fetchone()
    return int(row["version"]) if row is not None else None


def read_runtime_sqlite(db_path: str) -> Optional[Tuple[int, Dict[str, Any]]]:
    """``(version, runtime dict)`` of the committed row; None when absent."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT config_json, version FROM server_config WHERE config_key = ?",
            (CONFIG_KEY_RUNTIME,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[1]), _parsed(row[0])


def commit_runtime_pg(
    pool: Any, runtime_dict: Dict[str, Any], expected_version: int
) -> Optional[int]:
    """ONE atomic ``UPDATE ... AND version = %s RETURNING version``: the new
    version, or None when another process committed since that version."""
    from psycopg.rows import dict_row

    params: List[Any] = [
        json.dumps(runtime_dict),
        UPDATER_WEB_UI,
        CONFIG_KEY_RUNTIME,
        expected_version,
    ]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "UPDATE server_config"
                " SET config_json = jsonb_set("
                "         %s::jsonb,"
                "         '{launch_restart_generation}',"
                "         to_jsonb(COALESCE("
                "             (config_json->>'launch_restart_generation')::int,"
                "             0"
                "         ))"
                "     ),"
                "     version = version + 1,"
                "     updated_at = CURRENT_TIMESTAMP,"
                "     updated_by = %s"
                " WHERE config_key = %s AND version = %s"
                " RETURNING version",
                params,
            ).fetchone()
        conn.commit()
    return int(row["version"]) if row is not None else None


def commit_runtime_sqlite(
    db_path: str, runtime_dict: Dict[str, Any], expected_version: int
) -> Optional[int]:
    """Inside ONE ``BEGIN IMMEDIATE`` (the cross-process write guard):
    re-read the row and write only while it is at *expected_version*.  The
    new version, or None when another process committed since."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT config_json, version FROM server_config WHERE config_key = ?",
            (CONFIG_KEY_RUNTIME,),
        ).fetchone()
        if existing is None or int(existing[1]) != expected_version:
            conn.rollback()
            return None
        generation = int(_parsed(existing[0]).get("launch_restart_generation") or 0)
        preserved = dict(runtime_dict, launch_restart_generation=generation)
        conn.execute(
            "UPDATE server_config SET config_json = ?, version = version + 1, "
            "updated_at = datetime('now'), updated_by = ? WHERE config_key = ?",
            (json.dumps(preserved), UPDATER_WEB_UI, CONFIG_KEY_RUNTIME),
        )
        conn.commit()
    finally:
        conn.close()
    return expected_version + 1


def seed_runtime_pg(pool: Any, runtime_dict: Dict[str, Any]) -> bool:
    """First boot: insert the row when absent.  True only when inserted (a
    row another process seeded first is never touched)."""
    with pool.connection() as conn:
        inserted = conn.execute(
            "INSERT INTO server_config (config_key, config_json, version, updated_by) "
            "VALUES (%s, %s, 1, %s) ON CONFLICT (config_key) DO NOTHING",
            (CONFIG_KEY_RUNTIME, json.dumps(runtime_dict), UPDATER_SEED),
        ).rowcount
        conn.commit()
    return bool(inserted == 1)


def seed_runtime_sqlite(db_path: str, runtime_dict: Dict[str, Any]) -> bool:
    """First boot: insert the row when absent.  True only when inserted."""
    conn = _connect(db_path)
    try:
        inserted = conn.execute(
            "INSERT INTO server_config (config_key, config_json, version, updated_by) "
            "VALUES (?, ?, 1, ?) ON CONFLICT(config_key) DO NOTHING",
            (CONFIG_KEY_RUNTIME, json.dumps(runtime_dict), UPDATER_SEED),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    return bool(inserted == 1)
