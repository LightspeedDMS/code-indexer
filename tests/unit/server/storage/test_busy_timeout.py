import tempfile
import os

from code_indexer.server.storage.database_manager import DatabaseConnectionManager


def test_get_connection_sets_busy_timeout_to_30000():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        manager = DatabaseConnectionManager(db_path)
        conn = manager.get_connection()
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row is not None
        assert row[0] == 30000, f"Expected busy_timeout=30000, got {row[0]}"
    finally:
        os.unlink(db_path)
