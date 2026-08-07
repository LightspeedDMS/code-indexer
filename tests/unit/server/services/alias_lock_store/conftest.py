"""Shared fixtures for the PostgresAliasLockStore test modules (Issue #1546
Phase 1).

Schema is applied through the REAL migration path (``MigrationRunner``,
migration 043) once per test session -- the store itself no longer issues
its own CREATE TABLE (Fix #5: the migration file is the sole source of
truth for schema).
"""

from __future__ import annotations

import os
import uuid

import pytest

from code_indexer.server.services.alias_lock_store.postgres_store import (
    PostgresAliasLockStore,
)

postgres_skip_marker = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set; skipping real-PG alias lock store tests",
)


@pytest.fixture(scope="session", autouse=True)
def _apply_real_migrations():
    """Apply the REAL migration set (including migration 043, which
    creates golden_repo_alias_locks) via the project's own MigrationRunner
    -- never a store-private CREATE TABLE (Fix #5). Runs once per test
    session against TEST_POSTGRES_DSN; a no-op on every subsequent test
    module since MigrationRunner tracks applied migrations in
    schema_migrations and skips anything already applied.
    """
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        return
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    MigrationRunner(dsn).run()


@pytest.fixture
def pg_dsn() -> str:
    return os.environ["TEST_POSTGRES_DSN"]


@pytest.fixture
def store(pg_dsn: str) -> PostgresAliasLockStore:
    return PostgresAliasLockStore(pg_dsn)


@pytest.fixture
def unique_key() -> str:
    """A fresh, test-run-scoped lock_key -- avoids collisions across
    concurrent test runs against the same shared database."""
    return f"test-alias-lock-{uuid.uuid4().hex[:12]}"
