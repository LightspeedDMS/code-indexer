"""
Shared helpers for the Bug #1950 restart-interrupted-job test modules
(test_restart_interrupted_jobs_*_1950.py).

Not itself a test module (no test_ prefix) -- pytest does not collect it.
Extracted to avoid repeating the same SQLite schema-init + backend
construction boilerplate across every Bug #1950 test file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

# Arbitrary but named sentinel progress values used when seeding jobs --
# their exact value carries no meaning, only that they are non-zero/
# non-complete so a seeded "running" job looks realistic.
SEED_PROGRESS_MID = 42
SEED_PROGRESS_LOW = 5
SEED_PROGRESS_PARTIAL = 25


def make_sqlite_backend(tmp_path: Path, db_name: str = "test.db"):
    """Create a fresh BackgroundJobsSqliteBackend on a real,
    schema-initialized SQLite file under tmp_path."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend

    db_path = tmp_path / db_name
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    return BackgroundJobsSqliteBackend(str(db_path))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
