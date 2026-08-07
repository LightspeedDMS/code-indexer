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

_POSTGRES_TEST_MODULE_NAMES = (
    "test_alias_lock_store_postgres_1546",
    "test_alias_lock_store_postgres_concurrency_1546",
)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Round-3 review, low-cost fix: fail LOUDLY, not silently skip, when
    a PostgreSQL alias-lock-store test is EXPLICITLY selected on the
    command line but TEST_POSTGRES_DSN is unset. Without this, the
    critical Fix #1 (indefinite-block) regression gate had zero
    default-running proof -- another developer or CI could get an
    all-green run of this exact suite without ever exercising the
    PostgreSQL path. `tryfirst=True` ensures this runs before pytest's
    own skip-marker evaluation. When NOT explicitly selected (e.g. a
    broad sweep of the whole tests/ tree), the existing
    `postgres_skip_marker` skip behavior is preserved unchanged --
    matching this project's existing skip-gating posture for the other
    23+ live-PG test files elsewhere in this codebase.
    """
    if os.environ.get("TEST_POSTGRES_DSN"):
        return

    file_path = item.nodeid.split("::", 1)[0]
    module_name = file_path.rsplit("/", 1)[-1].removesuffix(".py")
    if module_name not in _POSTGRES_TEST_MODULE_NAMES:
        return

    invocation_args = " ".join(item.config.invocation_params.args).lower()
    explicitly_selected = "postgres" in invocation_args
    if not explicitly_selected:
        return

    pytest.fail(
        "TEST_POSTGRES_DSN is not set, but PostgreSQL alias-lock-store "
        "tests were explicitly selected on the command line (invocation "
        f"args: {invocation_args!r}). Set TEST_POSTGRES_DSN to a real "
        "PostgreSQL instance to run this suite for real -- it covers "
        "Fix #1 (indefinite-block regression), the most severe finding "
        "in Issue #1546's Phase 1 rework. Silently skipping an "
        "explicitly-requested test suite is not acceptable here.",
        pytrace=False,
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
