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
from pathlib import Path

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

# This conftest.py's own resolved directory -- the precise, path
# -boundary-aware comparison root for "does this collected item belong
# to the alias_lock_store suite" (see `_item_belongs_here` below).
_THIS_DIR = Path(__file__).resolve().parent

# Round-4 review fix: the original implementation pattern-matched the
# raw CLI argument text for the substring "postgres", which Codex
# proved bypassable -- `pytest tests/unit/server/services/
# alias_lock_store/` (the whole directory, no "postgres" substring
# anywhere in the invocation) still collected and silently skipped all
# 21 PostgreSQL tests. Collected-item-based detection (via
# pytest_collection_modifyitems below) cannot be bypassed this way: it
# looks at what pytest ACTUALLY gathered, not how the invocation was
# spelled. The computed flag lives on pytest's own per-session
# `config.stash` (never a bare module-level dict) so it is not
# unsynchronized shared mutable state.
_EXPLICIT_SCOPE_KEY: "pytest.StashKey[bool]" = pytest.StashKey()


def _item_belongs_here(item: pytest.Item) -> bool:
    """Precise, path-boundary-aware check: True only if `item`'s test
    file lives DIRECTLY in this exact directory (never a mere substring
    match on nodeid, which could false-positive on an unrelated sibling
    directory such as a hypothetical `alias_lock_store_backup/`)."""
    return item.path.resolve().parent == _THIS_DIR


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list
) -> None:
    """Round-4 review fix: decide "explicit" scope from the ACTUAL
    collected item list rather than CLI argument text. "Explicit" means
    every single collected item in this run belongs to this
    alias_lock_store directory (a file-specific, directory-specific, or
    -k-filtered selection that resolves ENTIRELY within this feature) --
    as opposed to a much broader sweep (e.g. server-fast-automation.sh's
    full `tests/unit/server/` run) that happens to sweep these tests up
    among hundreds of unrelated ones, where the existing skip-based
    tolerance for missing TEST_POSTGRES_DSN must be preserved."""
    config.stash[_EXPLICIT_SCOPE_KEY] = bool(items) and all(
        _item_belongs_here(item) for item in items
    )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Round-3 review, low-cost fix (narrowed in round-4 per Codex's
    finding): fail LOUDLY, not silently skip, when a PostgreSQL
    alias-lock-store test is EXPLICITLY selected (per the collected-item
    -based flag computed above) but TEST_POSTGRES_DSN is unset. Without
    this, the critical Fix #1 (indefinite-block) regression gate had
    zero default-running proof -- another developer or CI could get an
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

    if not item.config.stash.get(_EXPLICIT_SCOPE_KEY, False):
        return

    pytest.fail(
        "TEST_POSTGRES_DSN is not set, but this test run's ENTIRE "
        "collected scope is the alias_lock_store PostgreSQL/SQLite "
        "suite (no unrelated tests were swept in) -- that counts as an "
        "explicit request to run these PostgreSQL tests, whether "
        "selected by file, by directory, or by -k. Set "
        "TEST_POSTGRES_DSN to a real PostgreSQL instance to run this "
        "suite for real -- it covers Fix #1 (indefinite-block "
        "regression), the most severe finding in Issue #1546's Phase 1 "
        "rework. Silently skipping an explicitly-requested test suite "
        "is not acceptable here.",
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
