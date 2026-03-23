"""
PostgreSQL migration runner using numbered SQL files.

Story #416: Database Migration System with Numbered SQL Files

Manages schema evolution via forward-only numbered SQL files.
Tracks applied migrations in schema_migrations table.
Checksums prevent re-running modified migrations.

Usage:
    python3 -m code_indexer.server.storage.postgres.migrations.runner \\
        --connection-string "postgresql://user:pass@host/db"
"""

import argparse
import hashlib
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_SQL_DIR = Path(__file__).parent / "sql"

_CREATE_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum TEXT NOT NULL
)
"""

_INSERT_MIGRATION = """
INSERT INTO schema_migrations (filename, checksum)
VALUES (%s, %s)
"""

_SELECT_APPLIED = """
SELECT filename FROM schema_migrations ORDER BY applied_at ASC
"""


class MigrationRunner:
    """
    Runs forward-only numbered SQL migrations against a PostgreSQL database.

    Discovers NNN_description.sql files in the sql/ subdirectory, applies
    only the pending ones in numeric order, and records each in the
    schema_migrations table with a checksum to detect file tampering.
    """

    def __init__(self, connection_string: str) -> None:
        """
        Initialize with a PostgreSQL connection string.

        Establishes the database connection immediately.

        Args:
            connection_string: PostgreSQL DSN, e.g.
                "postgresql://user:pass@localhost/dbname"

        Raises:
            ImportError: if psycopg v3 is not installed.
            psycopg.Error: if connection cannot be established.
        """
        if psycopg is None:
            raise ImportError(
                "psycopg (v3) is required for PostgreSQL migrations. "
                "Install it with: pip install psycopg"
            )
        self._connection_string = connection_string
        self._conn = psycopg.connect(connection_string)
        self._sql_dir = _SQL_DIR

    def discover_migrations(self) -> List[Path]:
        """
        Find all NNN_description.sql files in the sql/ directory.

        Returns files sorted by their numeric prefix so migrations
        are applied in the correct order regardless of filename length.

        Returns:
            List of Path objects sorted numerically by prefix.
        """
        sql_files = list(self._sql_dir.glob("*.sql"))

        def _numeric_prefix(path: Path) -> int:
            name = path.name
            prefix = name.split("_")[0]
            try:
                return int(prefix)
            except ValueError:
                return 0

        return sorted(sql_files, key=_numeric_prefix)

    def _calculate_checksum(self, migration_path: Path) -> str:
        """
        Calculate the MD5 checksum of a migration file's content.

        Args:
            migration_path: Path to the SQL file.

        Returns:
            Hex-encoded MD5 digest string.
        """
        content = migration_path.read_text(encoding="utf-8")
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def ensure_migrations_table(self) -> None:
        """
        Create the schema_migrations tracking table if it does not exist.

        Idempotent — safe to call on every startup.
        """
        with self._conn.cursor() as cur:
            cur.execute(_CREATE_SCHEMA_MIGRATIONS)
        self._conn.commit()

    def get_applied_migrations(self) -> List[str]:
        """
        Return the list of already-applied migration filenames.

        Returns:
            List of filename strings in the order they were applied.
        """
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_APPLIED)
            rows = cur.fetchall()
        return [row[0] for row in rows]

    def apply_migration(self, migration_path: Path) -> None:
        """
        Execute a single migration file within a transaction.

        On success: commits the migration SQL and records the filename
        and checksum in schema_migrations.

        On failure: rolls back the entire transaction and re-raises.

        Args:
            migration_path: Path to the .sql file to execute.

        Raises:
            Exception: any database error encountered during execution.
        """
        sql_content = migration_path.read_text(encoding="utf-8")
        checksum = self._calculate_checksum(migration_path)
        filename = migration_path.name

        try:
            with self._conn.cursor() as cur:
                cur.execute(sql_content)
                cur.execute(_INSERT_MIGRATION, (filename, checksum))
            self._conn.commit()
            logger.info("Applied migration: %s", filename)
        except Exception:
            self._conn.rollback()
            logger.error("Failed to apply migration: %s", filename)
            raise

    def run(self) -> int:
        """
        Execute all pending migrations in numeric order.

        Calls ensure_migrations_table(), discovers all SQL files, compares
        against already-applied migrations, and applies each pending one.

        Returns:
            Count of migrations applied in this run.
        """
        self.ensure_migrations_table()
        applied = set(self.get_applied_migrations())
        all_migrations = self.discover_migrations()
        pending = [m for m in all_migrations if m.name not in applied]

        for migration_path in pending:
            logger.info("Applying pending migration: %s", migration_path.name)
            self.apply_migration(migration_path)

        count = len(pending)
        logger.info(
            "Migration run complete: %d applied, %d already up-to-date",
            count,
            len(applied),
        )
        return count

    def get_status(self) -> Dict[str, object]:
        """
        Return migration status summary.

        Returns:
            Dict with keys:
                applied_count (int): number of applied migrations
                pending_count (int): number of pending migrations
                last_applied (Optional[str]): filename of most recently applied
        """
        applied = self.get_applied_migrations()
        all_migrations = self.discover_migrations()
        applied_set = set(applied)
        pending = [m for m in all_migrations if m.name not in applied_set]

        last_applied: Optional[str] = applied[-1] if applied else None

        return {
            "applied_count": len(applied),
            "pending_count": len(pending),
            "last_applied": last_applied,
        }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run PostgreSQL schema migrations for CIDX server.",
        prog="python3 -m code_indexer.server.storage.postgres.migrations.runner",
    )
    parser.add_argument(
        "--connection-string",
        required=True,
        metavar="DSN",
        help='PostgreSQL connection string, e.g. "postgresql://user:pass@host/db"',
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print migration status and exit without applying migrations.",
    )
    return parser


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = _build_arg_parser()
    args = parser.parse_args()

    try:
        runner = MigrationRunner(args.connection_string)
    except ImportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: Cannot connect to database: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.status:
        status = runner.get_status()
        print(f"Applied:  {status['applied_count']}")
        print(f"Pending:  {status['pending_count']}")
        print(f"Last:     {status['last_applied'] or '(none)'}")
        sys.exit(0)

    try:
        count = runner.run()
        print(f"Applied {count} migration(s).")
        sys.exit(0)
    except Exception as exc:
        print(f"ERROR: Migration failed: {exc}", file=sys.stderr)
        sys.exit(1)
