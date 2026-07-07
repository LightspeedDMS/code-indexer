"""C1 worker-safe --workers: migrations serialize under a PG advisory lock.

Concurrent starters (multiple uvicorn workers, or pods) must not race on
CREATE TABLE IF NOT EXISTS schema_migrations / the INSERT into it. run() takes
a session-level advisory lock around the whole check-then-apply.
"""

from unittest.mock import MagicMock, patch


def _run_with_mock_conn():
    with patch("code_indexer.server.storage.postgres.migrations.runner.psycopg") as pg:
        conn = MagicMock()
        pg.connect.return_value = conn
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        runner = MigrationRunner("postgresql://x/y")
        # stub out the actual migration work -- we only assert the locking
        runner.ensure_migrations_table = MagicMock()
        runner.get_applied_migrations = MagicMock(return_value=[])
        runner.discover_migrations = MagicMock(return_value=[])
        applied = runner.run()
        cur = conn.cursor.return_value.__enter__.return_value
        sqls = [str(c.args[0]) for c in cur.execute.call_args_list]
        return applied, sqls


def test_run_acquires_and_releases_advisory_lock():
    applied, sqls = _run_with_mock_conn()
    assert applied == 0
    joined = " ".join(sqls)
    assert "pg_advisory_lock" in joined
    assert "pg_advisory_unlock" in joined
    # lock must come before unlock
    lock_i = next(i for i, s in enumerate(sqls) if "pg_advisory_lock" in s)
    unlock_i = next(i for i, s in enumerate(sqls) if "pg_advisory_unlock" in s)
    assert lock_i < unlock_i
