"""Consume ``migrations()``: per-feature schema migrations and version rows.

Every loaded feature with migrations gets one row in
``feature_schema_versions`` (feature name, newest applied version). At app
creation, right after the core's own schema is ready, the pending
migrations run in dependency order against the active backend:

* PostgreSQL (cluster): ``MigrationRunner.run_feature_migrations`` under the
  runner's session advisory lock, so concurrent workers and nodes apply each
  migration exactly once;
* SQLite (solo): :func:`run_feature_migrations_sqlite`, each migration and
  its version bump in one ``BEGIN IMMEDIATE`` transaction (serialises the
  workers of the one host).

Rules for a feature's ``Migration.apply(connection)``: backward compatible
only (create if not exists, add nullable or defaulted columns; never drop,
rename or retype -- rolling restarts share the schema), and never commit or
roll back itself (the runner does). Removing a feature leaves its tables and
its version row in place, inert.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Dict, Iterator, List, Sequence, Tuple

from cidx_sdk import Migration

from code_indexer.server.features.loader import LoadedFeature

logger = logging.getLogger(__name__)

VERSIONS_TABLE = "feature_schema_versions"

CREATE_VERSIONS_TABLE_PG = f"""
CREATE TABLE IF NOT EXISTS {VERSIONS_TABLE} (
    feature TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""
SELECT_VERSION_PG = f"SELECT version FROM {VERSIONS_TABLE} WHERE feature = %s"
UPSERT_VERSION_PG = f"""
INSERT INTO {VERSIONS_TABLE} (feature, version) VALUES (%s, %s)
ON CONFLICT (feature) DO UPDATE SET version = EXCLUDED.version, updated_at = NOW()
"""

CREATE_VERSIONS_TABLE_SQLITE = f"""
CREATE TABLE IF NOT EXISTS {VERSIONS_TABLE} (
    feature TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
SELECT_VERSION_SQLITE = f"SELECT version FROM {VERSIONS_TABLE} WHERE feature = ?"
UPSERT_VERSION_SQLITE = f"""
INSERT INTO {VERSIONS_TABLE} (feature, version) VALUES (?, ?)
ON CONFLICT (feature) DO UPDATE SET version = excluded.version,
    updated_at = CURRENT_TIMESTAMP
"""

#: Feature name -> its migrations, in dependency order.
MigrationPlan = List[Tuple[str, Sequence[Migration]]]


def migration_plan(loaded: Sequence[LoadedFeature]) -> MigrationPlan:
    """The features with migrations, in the registry's dependency order."""
    return [
        (item.manifest.name, item.contributions.migrations)
        for item in loaded
        if item.contributions.migrations
    ]


def pending_migrations(
    feature: str, migrations: Sequence[Migration], current: int
) -> Iterator[Migration]:
    """The migrations of *feature* newer than *current*, oldest first."""
    newest = max((m.version for m in migrations), default=0)
    if current > newest:
        logger.warning(
            "feature %s schema is at version %d, newer than its newest "
            "migration %d (downgraded add-on); applying nothing",
            feature,
            current,
            newest,
        )
        return iter(())
    return iter([m for m in migrations if m.version > current])


def run_feature_migrations_sqlite(db_path: str, plan: MigrationPlan) -> Dict[str, int]:
    """Apply *plan* to the SQLite database at *db_path*. Blocking.

    Returns:
        Feature name -> number of migrations applied.

    Raises:
        Exception: the failing migration's error, after rolling it back.
    """
    applied: Dict[str, int] = {}
    if not plan:
        return applied
    conn = sqlite3.connect(db_path, timeout=60.0, isolation_level=None)
    try:
        conn.execute(CREATE_VERSIONS_TABLE_SQLITE)
        for feature, migrations in plan:
            count = 0
            for migration in migrations:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(SELECT_VERSION_SQLITE, (feature,)).fetchone()
                    current = int(row[0]) if row else 0
                    if not list(pending_migrations(feature, [migration], current)):
                        conn.execute("COMMIT")
                        continue
                    migration.apply(conn)
                    conn.execute(UPSERT_VERSION_SQLITE, (feature, migration.version))
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    logger.error(
                        "Feature migration failed: %s %d", feature, migration.version
                    )
                    raise
                logger.info(
                    "Applied feature migration: %s %d (%s)",
                    feature,
                    migration.version,
                    migration.description,
                )
                count += 1
            applied[feature] = count
    finally:
        conn.close()
    return applied


def feature_schema_versions_sqlite(db_path: str) -> Dict[str, int]:
    """Every feature version row of the SQLite database (empty if none)."""
    conn = sqlite3.connect(db_path, timeout=60.0)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (VERSIONS_TABLE,),
        ).fetchone()
        if not exists:
            return {}
        rows = conn.execute(f"SELECT feature, version FROM {VERSIONS_TABLE}")
        return {str(name): int(version) for name, version in rows}
    finally:
        conn.close()


def run_feature_migrations(
    loaded: Sequence[LoadedFeature],
    *,
    storage_mode: str,
    postgres_dsn: str,
    sqlite_path: str,
) -> Dict[str, int]:
    """Apply every loaded feature's pending migrations to the active backend.

    Blocking; runs while the app is created, after the core schema exists.
    """
    plan = migration_plan(loaded)
    if not plan:
        return {}
    if storage_mode == "postgres":
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        with MigrationRunner(postgres_dsn) as runner:
            applied: Dict[str, int] = runner.run_feature_migrations(plan)
        return applied
    return run_feature_migrations_sqlite(sqlite_path, plan)
