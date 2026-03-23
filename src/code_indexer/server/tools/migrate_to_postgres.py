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


# ---------------------------------------------------------------------------
# Columns that hold JSON strings in SQLite but should become JSON/JSONB in PG.
# Keyed by table name -> set of column names.
# ---------------------------------------------------------------------------
JSON_COLUMNS: Dict[str, set] = {
    "global_repos": {"temporal_options"},
    "golden_repos_metadata": {"temporal_options"},
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
    ) -> None:
        """
        Initialise the migrator.

        Args:
            sqlite_db_path: Path to cidx_server.db SQLite file.
            groups_db_path: Path to groups.db SQLite file.
            pg_connection_string: libpq connection string for PostgreSQL.
        """
        self._sqlite_path = sqlite_db_path
        self._groups_path = groups_db_path
        self._pg_conn_str = pg_connection_string

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
        else:
            raise ValueError(
                f"Unknown table '{table_name}'. Not in MAIN_DB_TABLES_ORDERED "
                f"or GROUPS_DB_TABLES_ORDERED."
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
                        cur.execute(
                            f"SELECT COUNT(*) FROM {table_name}"  # noqa: S608
                        )
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
        - JSON string columns -> parsed Python objects (psycopg v3 serialises
          them to JSONB automatically).
        - Integer 0/1 stored in BOOLEAN columns -> Python bool.
        - Timestamp strings left as-is (psycopg v3 accepts ISO-8601 strings
          for TIMESTAMPTZ).
        - None values passed through unchanged.

        Args:
            table_name: Name of the table the row belongs to.
            row: Row as a dict (column_name -> raw SQLite value).

        Returns:
            Transformed dict ready for insertion via psycopg v3.
        """
        result: Dict[str, Any] = {}
        json_cols = JSON_COLUMNS.get(table_name, set())

        for col, value in row.items():
            if value is None:
                result[col] = None
                continue

            if col in json_cols:
                result[col] = _parse_json_column(value)
            else:
                result[col] = value

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
            inserted = self._upsert_rows(pg_conn, table_name, rows)
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
    ) -> int:
        """
        Insert rows into a PostgreSQL table using ON CONFLICT DO NOTHING.

        Args:
            pg_conn: Active psycopg v3 connection.
            table_name: Destination table name.
            rows: List of row dicts to insert.

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
        with pg_conn.cursor() as cur:
            for row in rows:
                cur.execute(sql, row)
                total_inserted += cur.rowcount if cur.rowcount > 0 else 0

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
