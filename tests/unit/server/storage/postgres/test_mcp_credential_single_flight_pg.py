"""The analysis agents' credential get-or-create holds a cluster-wide
PostgreSQL advisory lock (skipped without TEST_POSTGRES_DSN)."""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from code_indexer.server.services.mcp_self_registration_service import (
    CREDENTIAL_LOCK_KEY,
    MCPSelfRegistrationService,
)


@pytest.fixture
def pool(migrated_scratch_pg_dsn: str) -> Iterator[Any]:
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    p = ConnectionPool(migrated_scratch_pg_dsn, min_size=1, max_size=4, name="t")
    yield p
    p.close()


def _other_pod_gets_the_lock(dsn: str) -> bool:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))", (CREDENTIAL_LOCK_KEY,)
        ).fetchone()
        got = bool(row and row[0])
        if got:
            conn.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))", (CREDENTIAL_LOCK_KEY,)
            )
    return got


def test_a_second_pod_waits_while_one_creates(
    pool: Any, migrated_scratch_pg_dsn: str
) -> None:
    service = MCPSelfRegistrationService(
        config_manager=None, mcp_credential_manager=None
    )
    service.set_connection_pool(pool)
    with service._single_flight():
        assert _other_pod_gets_the_lock(migrated_scratch_pg_dsn) is False
    assert _other_pod_gets_the_lock(migrated_scratch_pg_dsn) is True
