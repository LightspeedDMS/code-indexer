"""PostgreSQL alias-lock schema for a test, on any database.

Applies the real migration set (``MigrationRunner``, idempotent) so a test
file needs no other suite to have created ``golden_repo_alias_locks`` first,
and clears the test's own lock rows before and after.
"""

from __future__ import annotations

import os
from contextlib import closing, contextmanager
from typing import Iterator, Tuple

import pytest


def _clear(dsn: str, lock_keys: Tuple[str, ...]) -> None:
    import psycopg

    with closing(psycopg.connect(dsn)) as conn:
        with conn:
            with conn.cursor() as cur:
                for key in lock_keys:
                    cur.execute(
                        "DELETE FROM golden_repo_alias_locks WHERE lock_key = %s",
                        (key,),
                    )


@contextmanager
def postgres_lock_store_dsn(*lock_keys: str) -> Iterator[str]:
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    MigrationRunner(dsn).run()
    _clear(dsn, lock_keys)
    try:
        yield dsn
    finally:
        _clear(dsn, lock_keys)
