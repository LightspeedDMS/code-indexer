"""
SQLite-to-PostgreSQL data migration tool.

Story #418: SQLite-to-PostgreSQL Data Migration Tool

Reads all data from SQLite databases (cidx_server.db and groups.db) and
writes it into PostgreSQL. Validates data integrity after migration.
Idempotent (safe to re-run).

Usage:
    python3 -m code_indexer.server.tools.migrate_to_postgres \
      --sqlite-path ~/.cidx-server/data/cidx_server.db \
      --groups-path ~/.cidx-server/groups.db \
      --pg-url postgresql://user:pass@host/db
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tables sourced from cidx_server.db, in dependency order
# (parents before children to satisfy FK constraints on insert).
# ---------------------------------------------------------------------------
MAIN_DB_TABLES_ORDERED: List[str] = [
    "users",
    "user_api_keys",
    "user_mcp_credentials",
    "user_oidc_identities",
    "invalidated_sessions",
    "password_change_timestamps",
    "repo_categories",
    "global_repos",
    "golden_repos_metadata",
    "background_jobs",
    "sync_jobs",
    "ci_tokens",
    "ssh_keys",
    "ssh_key_hosts",
    "description_refresh_tracking",
    "dependency_map_tracking",
    "self_monitoring_scans",
    "self_monitoring_issues",
    "research_sessions",
    "research_messages",
    "diagnostic_results",
    "wiki_cache",
    "wiki_sidebar_cache",
    "user_git_credentials",
]

# Tables sourced from groups.db, in dependency order.
GROUPS_DB_TABLES_ORDERED: List[str] = [
    "groups",
    "user_group_membership",
    "repo_group_access",
    "audit_logs",
]

# Tables sourced from oauth.db, in dependency order.
# oauth_clients must come first to satisfy FK constraints from oauth_codes and oauth_tokens.
OAUTH_DB_TABLES_ORDERED: List[str] = [
    "oauth_clients",
    "oauth_codes",
    "oauth_tokens",
    "oidc_identity_links",
]

# Tables sourced from scip_audit.db (Story #516).
SCIP_AUDIT_DB_TABLES_ORDERED: List[str] = [
    "scip_dependency_installations",
]

# Tables sourced from refresh_tokens.db (Story #515), in dependency order.
# token_families must come first to satisfy FK constraints from refresh_tokens.
REFRESH_TOKEN_DB_TABLES_ORDERED: List[str] = [
    "token_families",
    "refresh_tokens",
]


# ---------------------------------------------------------------------------
# Columns that hold JSON strings in SQLite but should become JSON/JSONB in PG.
# Keyed by table name -> set of column names.
# ---------------------------------------------------------------------------
JSON_COLUMNS: Dict[str, set] = {
    "global_repos": {"temporal_options"},
    "golden_repos_metadata": {"temporal_options"},
    "oauth_clients": {"redirect_uris", "metadata"},
    "sync_jobs": {
        "phases",
        "phase_weights",
        "progress_history",
        "recovery_checkpoint",
        "analytics_data",
    },
    "background_jobs": {
        "result",
        "claude_actions",
        "extended_error",
        "language_resolution_status",
        "metadata",
    },
    "dependency_map_tracking": {"commit_hashes"},
    "diagnostic_results": {"results_json"},
    "wiki_sidebar_cache": {"sidebar_json"},
}

# ---------------------------------------------------------------------------
# Columns that hold ISO-8601 text timestamps in SQLite.
# These need no conversion — psycopg v3 accepts ISO strings for TIMESTAMPTZ
# as long as they are valid. We normalise None -> None and strip the value.
# ---------------------------------------------------------------------------
# (kept as documentation; transformation is handled in _transform_row)


# ---------------------------------------------------------------------------
# Columns that store INTEGER 0/1 in SQLite but should be BOOLEAN in PG.
# ---------------------------------------------------------------------------
BOOLEAN_COLUMNS: Dict[str, set] = {
    "global_repos": {"enable_temporal", "enable_scip", "wiki_enabled"},
    "oauth_codes": {"used"},
    "golden_repos_metadata": {
        "enable_temporal",
        "enable_scip",
        "wiki_enabled",
        "category_auto_assigned",
    },
    "ssh_keys": {"is_imported"},
    "groups": {"is_default"},
    "background_jobs": {"is_admin", "cancelled"},
    "users": {"is_admin", "is_sso"},
    "token_families": {"is_revoked"},
    "refresh_tokens": {"is_used"},
}

# ---------------------------------------------------------------------------
# Column renames: SQLite column name -> PG column name.
# Used when the migration SQL uses a different name than the SQLite schema.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Columns that store Unix epoch floats in SQLite but need ISO-8601 for PG.
# ---------------------------------------------------------------------------
EPOCH_TIMESTAMP_COLUMNS: Dict[str, set] = {
    "global_repos": {"next_refresh", "last_refresh"},
}

COLUMN_RENAMES: Dict[str, Dict[str, str]] = {
    "wiki_cache": {"metadata_json": "metadata"},
}


class SqliteToPostgresMigrator:
    """
    Migrates all data from SQLite (cidx_server.db + groups.db) to PostgreSQL.

    Idempotent: uses INSERT ... ON CONFLICT DO NOTHING so re-running is safe.
    Both the sqlite3 and psycopg imports are lazy (inside methods) to keep
    module-level import cost near zero.
    """

    def __init__(
        self,
        sqlite_db_path: str,
        groups_db_path: str,
        pg_connection_string: str,
        oauth_db_path: Optional[str] = None,
        scip_audit_db_path: Optional[str] = None,
        refresh_tokens_db_path: Optional[str] = None,
    ) -> None:
        """
        Initialise the migrator.

        Args:
            sqlite_db_path: Path to cidx_server.db SQLite file.
            groups_db_path: Path to groups.db SQLite file.
            pg_connection_string: libpq connection string for PostgreSQL.
            oauth_db_path: Optional path to oauth.db SQLite file.
            scip_audit_db_path: Optional path to scip_audit.db SQLite file (Story #516).
            refresh_tokens_db_path: Optional path to refresh_tokens.db SQLite file (Story #515).
        """
        self._sqlite_path = sqlite_db_path
        self._groups_path = groups_db_path
        self._pg_conn_str = pg_connection_string
        self._oauth_path = oauth_db_path
        self._scip_audit_path = scip_audit_db_path
        self._refresh_tokens_path = refresh_tokens_db_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def migrate_all(self) -> Dict[str, Any]:
        """
        Migrate all tables from both SQLite databases to PostgreSQL.

        Returns:
            A migration report dict::

                {
                    "tables": {
                        "users": {"rows_migrated": 5, "status": "ok"},
                        ...
                    },
                    "total_rows": N,
                }
        """
        report: Dict[str, Any] = {"tables": {}, "total_rows": 0}

        all_tables: List[Tuple[str, str]] = [
            (t, self._sqlite_path) for t in MAIN_DB_TABLES_ORDERED
        ] + [(t, self._groups_path) for t in GROUPS_DB_TABLES_ORDERED]

        if self._oauth_path:
            all_tables += [(t, self._oauth_path) for t in OAUTH_DB_TABLES_ORDERED]

        if self._scip_audit_path:
            all_tables += [
                (t, self._scip_audit_path) for t in SCIP_AUDIT_DB_TABLES_ORDERED
            ]

        if self._refresh_tokens_path:
            all_tables += [
                (t, self._refresh_tokens_path) for t in REFRESH_TOKEN_DB_TABLES_ORDERED
            ]

        for table_name, db_path in all_tables:
            try:
                rows_migrated = self._migrate_table_from(table_name, db_path)
                report["tables"][table_name] = {
                    "rows_migrated": rows_migrated,
                    "status": "ok",
                }
                report["total_rows"] += rows_migrated
                logger.info(f"Migrated table '{table_name}': {rows_migrated} rows")
            except Exception as exc:
                report["tables"][table_name] = {
                    "rows_migrated": 0,
                    "status": f"error: {exc}",
                }
                logger.error(f"Failed to migrate table '{table_name}': {exc}")

        return report

    def migrate_table(self, table_name: str) -> int:
        """
        Migrate a single table from the appropriate SQLite database.

        Chooses cidx_server.db or groups.db based on the table name.

        Args:
            table_name: Name of the table to migrate.

        Returns:
            Number of rows migrated.

        Raises:
            ValueError: If the table name is not recognised.
        """
        if table_name in MAIN_DB_TABLES_ORDERED:
            db_path = self._sqlite_path
        elif table_name in GROUPS_DB_TABLES_ORDERED:
            db_path = self._groups_path
        elif table_name in OAUTH_DB_TABLES_ORDERED:
            if not self._oauth_path:
                raise ValueError(
                    f"Table '{table_name}' is an OAuth table but oauth_db_path was not provided."
                )
            db_path = self._oauth_path
        elif table_name in SCIP_AUDIT_DB_TABLES_ORDERED:
            if not self._scip_audit_path:
                raise ValueError(
                    f"Table '{table_name}' is a SCIP audit table but scip_audit_db_path was not provided."
                )
            db_path = self._scip_audit_path
        elif table_name in REFRESH_TOKEN_DB_TABLES_ORDERED:
            if not self._refresh_tokens_path:
                raise ValueError(
                    f"Table '{table_name}' is a refresh token table but refresh_tokens_db_path was not provided."
                )
            db_path = self._refresh_tokens_path
        else:
            raise ValueError(
                f"Unknown table '{table_name}'. Not in MAIN_DB_TABLES_ORDERED, "
                f"GROUPS_DB_TABLES_ORDERED, OAUTH_DB_TABLES_ORDERED, SCIP_AUDIT_DB_TABLES_ORDERED, "
                f"or REFRESH_TOKEN_DB_TABLES_ORDERED."
            )
        return self._migrate_table_from(table_name, db_path)

    def validate(self) -> Dict[str, Any]:
        """
        Compare row counts between SQLite and PostgreSQL for every table.

        Returns:
            A validation report dict::

                {
                    "tables": {
                        "users": {
                            "sqlite_count": 5,
                            "pg_count": 5,
                            "match": True,
                        },
                        ...
                    },
                    "all_match": True,
                }
        """
        import sqlite3 as _sqlite3

        report: Dict[str, Any] = {"tables": {}, "all_match": True}

        pg_conn = self._get_pg_connection()
        try:
            all_tables: List[Tuple[str, str]] = [
                (t, self._sqlite_path) for t in MAIN_DB_TABLES_ORDERED
            ] + [(t, self._groups_path) for t in GROUPS_DB_TABLES_ORDERED]

            if self._oauth_path:
                all_tables += [(t, self._oauth_path) for t in OAUTH_DB_TABLES_ORDERED]

            if self._scip_audit_path:
                all_tables += [
                    (t, self._scip_audit_path) for t in SCIP_AUDIT_DB_TABLES_ORDERED
                ]

            if self._refresh_tokens_path:
                all_tables += [
                    (t, self._refresh_tokens_path)
                    for t in REFRESH_TOKEN_DB_TABLES_ORDERED
                ]

            for table_name, db_path in all_tables:
                # SQLite count
                sqlite_count: Optional[int] = None
                try:
                    sq_conn = _sqlite3.connect(db_path)
                    try:
                        sq_cursor = sq_conn.execute(
                            f"SELECT COUNT(*) FROM {table_name}"
                        )
                        sqlite_count = sq_cursor.fetchone()[0]
                    except _sqlite3.OperationalError:
                        # Table may not exist in older SQLite databases.
                        sqlite_count = 0
                    finally:
                        sq_conn.close()
                except Exception as exc:
                    logger.warning(
                        f"Cannot read SQLite count for '{table_name}': {exc}"
                    )
                    sqlite_count = None

                # PostgreSQL count
                pg_count: Optional[int] = None
                try:
                    with pg_conn.cursor() as cur:
                        cur.execute(f"SELECT COUNT(*) FROM {table_name}")  # noqa: S608
                        row = cur.fetchone()
                        pg_count = row[0] if row else 0
                except Exception as exc:
                    logger.warning(f"Cannot read PG count for '{table_name}': {exc}")
                    pg_count = None

                match = (
                    sqlite_count is not None
                    and pg_count is not None
                    and sqlite_count == pg_count
                )
                report["tables"][table_name] = {
                    "sqlite_count": sqlite_count,
                    "pg_count": pg_count,
                    "match": match,
                }
                if not match:
                    report["all_match"] = False

        finally:
            pg_conn.close()

        return report

    # ------------------------------------------------------------------
    # Schema discovery
    # ------------------------------------------------------------------

    def _get_sqlite_tables(self, db_path: Optional[str] = None) -> List[str]:
        """
        Discover all user tables in a SQLite database.

        Args:
            db_path: Path to the SQLite file. Defaults to the main
                     cidx_server.db path.

        Returns:
            Sorted list of table names present in the database.
        """
        import sqlite3 as _sqlite3

        path = db_path if db_path is not None else self._sqlite_path
        conn = _sqlite3.connect(path)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
            return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Row transformation
    # ------------------------------------------------------------------

    def _transform_row(self, table_name: str, row: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transform a SQLite row dict to a PostgreSQL-compatible dict.

        Conversions applied:
        - JSON string columns -> psycopg Json() wrapper for JSONB insertion.
        - Integer 0/1 stored in BOOLEAN columns -> Python bool.
        - Column renames (e.g. metadata_json -> metadata).
        - Timestamp strings left as-is (psycopg v3 accepts ISO-8601 strings).
        - None values passed through unchanged.
        """
        from psycopg.types.json import Json

        result: Dict[str, Any] = {}
        json_cols = JSON_COLUMNS.get(table_name, set())
        bool_cols = BOOLEAN_COLUMNS.get(table_name, set())
        renames = COLUMN_RENAMES.get(table_name, {})

        for col, value in row.items():
            # Apply column rename if needed
            dest_col = renames.get(col, col)

            if value is None:
                result[dest_col] = None
                continue

            if col in json_cols:
                parsed = _parse_json_column(value)
                result[dest_col] = Json(parsed) if parsed is not None else None
            elif col in bool_cols:
                result[dest_col] = bool(value)
            else:
                result[dest_col] = value

        # Convert epoch timestamps to ISO-8601
        epoch_cols = EPOCH_TIMESTAMP_COLUMNS.get(table_name, set())
        for col in epoch_cols:
            dest = renames.get(col, col)
            val = result.get(dest)
            if val is not None:
                # Epoch can be stored as int, float, or numeric string
                try:
                    epoch_val = float(val)
                    # Only convert if it looks like an epoch (> year 2000 in seconds)
                    if epoch_val > 946684800:
                        from datetime import datetime, timezone

                        result[dest] = datetime.fromtimestamp(
                            epoch_val, tz=timezone.utc
                        ).isoformat()
                except (ValueError, TypeError, OSError):
                    pass  # Not an epoch — leave as-is (already ISO string)

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _migrate_table_from(self, table_name: str, db_path: str) -> int:
        """
        Read all rows from *table_name* in *db_path* and upsert into PG.

        Uses INSERT ... ON CONFLICT DO NOTHING for idempotency.

        Returns:
            Number of rows inserted (conflicts not counted).
        """
        import sqlite3 as _sqlite3

        rows = self._read_sqlite_table(table_name, db_path, _sqlite3)
        if not rows:
            return 0

        pg_conn = self._get_pg_connection()
        try:
            # Try to disable FK triggers for this table (requires table owner)
            fk_disabled = False
            try:
                pg_conn.execute(
                    f"ALTER TABLE {table_name} DISABLE TRIGGER ALL"  # noqa: S608
                )
                fk_disabled = True
            except Exception as exc:
                logger.debug(
                    "Could not disable triggers for %s (will skip FK errors per-row): %s",
                    table_name,
                    exc,
                )
                pg_conn.rollback()  # Reset after failed ALTER

            inserted = self._upsert_rows(
                pg_conn,
                table_name,
                rows,
                skip_fk_errors=not fk_disabled,
            )

            if fk_disabled:
                pg_conn.execute(
                    f"ALTER TABLE {table_name} ENABLE TRIGGER ALL"  # noqa: S608
                )
            pg_conn.commit()
        except Exception:
            pg_conn.rollback()
            pg_conn.close()
            raise
        else:
            pg_conn.close()

        return inserted

    def _read_sqlite_table(
        self,
        table_name: str,
        db_path: str,
        sqlite3_module: Any,
    ) -> List[Dict[str, Any]]:
        """
        Read all rows from a SQLite table as a list of dicts.

        Returns an empty list if the table does not exist.
        """
        conn = sqlite3_module.connect(db_path)
        conn.row_factory = sqlite3_module.Row
        try:
            try:
                cursor = conn.execute(f"SELECT * FROM {table_name}")  # noqa: S608
            except sqlite3_module.OperationalError:
                # Table does not exist in this SQLite DB.
                return []
            raw_rows = cursor.fetchall()
            return [self._transform_row(table_name, dict(row)) for row in raw_rows]
        finally:
            conn.close()

    def _upsert_rows(
        self,
        pg_conn: Any,
        table_name: str,
        rows: List[Dict[str, Any]],
        skip_fk_errors: bool = False,
    ) -> int:
        """
        Insert rows into a PostgreSQL table using ON CONFLICT DO NOTHING.

        Args:
            pg_conn: Active psycopg v3 connection.
            table_name: Destination table name.
            rows: List of row dicts to insert.
            skip_fk_errors: When True, catch FK violation errors per-row
                using SAVEPOINTs and log a warning instead of failing.

        Returns:
            Total number of rows affected (inserted, not conflicting).
        """
        if not rows:
            return 0

        columns = list(rows[0].keys())
        col_list = ", ".join(columns)
        placeholders = ", ".join(f"%({col})s" for col in columns)
        sql = (
            f"INSERT INTO {table_name} ({col_list}) "  # noqa: S608
            f"VALUES ({placeholders}) "
            f"ON CONFLICT DO NOTHING"
        )

        total_inserted = 0
        skipped = 0
        with pg_conn.cursor() as cur:
            for row in rows:
                if skip_fk_errors:
                    from psycopg.errors import ForeignKeyViolation

                    try:
                        cur.execute("SAVEPOINT row_sp")
                        cur.execute(sql, row)
                        cur.execute("RELEASE SAVEPOINT row_sp")
                        total_inserted += cur.rowcount if cur.rowcount > 0 else 0
                    except ForeignKeyViolation as exc:
                        cur.execute("ROLLBACK TO SAVEPOINT row_sp")
                        logger.debug(
                            "Skipped row in '%s': %s",
                            table_name,
                            exc,
                        )
                        skipped += 1
                else:
                    cur.execute(sql, row)
                    total_inserted += cur.rowcount if cur.rowcount > 0 else 0

        if skipped > 0:
            logger.warning(
                "Skipped %d rows in '%s' due to FK constraint violations",
                skipped,
                table_name,
            )

        return total_inserted

    def _get_pg_connection(self) -> Any:
        """
        Open and return a new psycopg v3 synchronous connection.

        Raises:
            ImportError: If psycopg (v3) is not installed.
        """
        try:
            import psycopg  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "psycopg (v3) is required for SqliteToPostgresMigrator. "
                "Install with: pip install psycopg"
            ) from exc

        return psycopg.connect(self._pg_conn_str)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _parse_json_column(value: Any) -> Any:
    """
    Parse a JSON string column value.

    If the value is already a dict/list (e.g., already deserialised),
    return it as-is. If it is a string, attempt JSON parsing. On failure,
    return the original string so the migration does not abort.
    """
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_arg_parser():  # type: ignore[return]
    """Build the argument parser for the CLI."""
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="migrate_to_postgres",
        description="Migrate CIDX server data from SQLite to PostgreSQL.",
    )
    parser.add_argument(
        "--sqlite-path",
        required=True,
        help="Path to cidx_server.db SQLite file.",
    )
    parser.add_argument(
        "--groups-path",
        required=True,
        help="Path to groups.db SQLite file.",
    )
    parser.add_argument(
        "--pg-url",
        required=True,
        help="PostgreSQL connection URL (e.g. postgresql://user:pass@host/db).",
    )
    parser.add_argument(
        "--oauth-path",
        default=None,
        help="Path to oauth.db SQLite file (optional).",
    )
    parser.add_argument(
        "--scip-audit-path",
        default=None,
        help="Path to scip_audit.db SQLite file (optional, Story #516).",
    )
    parser.add_argument(
        "--refresh-tokens-path",
        default=None,
        help="Path to refresh_tokens.db SQLite file (optional, Story #515).",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        default=False,
        help="Only run validation (row count comparison), do not migrate.",
    )
    parser.add_argument(
        "--table",
        default=None,
        help="Migrate only this table (optional).",
    )
    return parser


def main() -> None:
    """CLI entry point for the migration tool."""
    import sys  # noqa: PLC0415

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = _build_arg_parser()
    args = parser.parse_args()

    migrator = SqliteToPostgresMigrator(
        sqlite_db_path=args.sqlite_path,
        groups_db_path=args.groups_path,
        pg_connection_string=args.pg_url,
        oauth_db_path=getattr(args, "oauth_path", None),
        scip_audit_db_path=getattr(args, "scip_audit_path", None),
        refresh_tokens_db_path=getattr(args, "refresh_tokens_path", None),
    )

    if args.validate_only:
        report = migrator.validate()
        _print_validation_report(report)
        if not report["all_match"]:
            sys.exit(1)
        return

    if args.table:
        try:
            count = migrator.migrate_table(args.table)
            print(f"Migrated {count} rows from table '{args.table}'.")
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        report = migrator.migrate_all()
        _print_migration_report(report)

    # Always run validation after migration.
    print("\nRunning post-migration validation...")
    validation = migrator.validate()
    _print_validation_report(validation)
    if not validation["all_match"]:
        print("WARNING: Row counts do not match. Review errors above.")
        sys.exit(1)
    print("Migration complete. All row counts match.")


def _print_migration_report(report: Dict[str, Any]) -> None:
    """Print a human-readable migration report to stdout."""
    print(f"\nMigration report (total rows: {report['total_rows']}):")
    for table, info in report["tables"].items():
        status = info["status"]
        rows = info["rows_migrated"]
        print(f"  {table}: {rows} rows — {status}")


def _print_validation_report(report: Dict[str, Any]) -> None:
    """Print a human-readable validation report to stdout."""
    all_match = report["all_match"]
    print(f"\nValidation report (all_match={all_match}):")
    for table, info in report["tables"].items():
        sq = info["sqlite_count"]
        pg = info["pg_count"]
        match = info["match"]
        marker = "OK" if match else "MISMATCH"
        print(f"  {table}: sqlite={sq} pg={pg} [{marker}]")


if __name__ == "__main__":
    main()
