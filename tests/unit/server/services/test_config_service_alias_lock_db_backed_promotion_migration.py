"""Regression tests: Issue #1546 Phase 3 default-promotion migration.

Phase 3 changed AliasLockConfig.db_backed_enabled's dataclass DEFAULT from
False to True. Runtime config is persisted as a full JSON blob and merged
OVER the dataclass default on load (ConfigService._merge_runtime_config), so
a deployment that already has a stored `alias_lock_config` section from
before Phase 3 shipped silently keeps the old False value forever -- a
stored value always beats a dataclass default. Confirmed inert on a live
3-node staging cluster: all three nodes still used file-based locking after
upgrading to the release that flipped the default.

These tests reproduce that exact scenario against a real on-disk SQLite
`server_config` row (the same storage ConfigService.initialize_runtime_db
reads in solo mode) and prove:

1. A stored blob that predates Phase 3 (explicit `alias_lock_config`
   section, no promotion marker) is promoted to True on load. This
   scenario deliberately ALSO omits `lifecycle_analysis_config` from the
   seed row (a very real combination -- a row old enough to predate
   Phase 3 very plausibly also predates lifecycle_analysis_config) to
   exercise a genuine interaction bug this migration exposed:
   initialize_runtime_db's lifecycle_analysis_config auto-migration
   (Story #885 A7d) used to re-save a STALE pre-merge copy of the runtime
   dict, which would have silently clobbered this migration's own write.

2. A stored blob representing an operator's deliberate post-promotion
   rollback to False (promotion marker already True) is NEVER touched
   again -- not just the value, but the row itself must not be rewritten
   at all (proven via an unchanged DB version and a byte-identical raw
   JSON string before/after). `lifecycle_analysis_config` is present in
   this seed specifically so the unrelated A7d backfill never fires,
   keeping the "zero writes when already promoted" invariant unambiguous
   (not conflated with a write caused by something else entirely).

3. A stored blob with no `alias_lock_config` section at all (never even
   reached Phase 2) needs no promotion write -- ServerConfig.__post_init__
   already constructs a fresh, modern-default AliasLockConfig() for it,
   and firing an unnecessary write here would silently break the
   pre-existing version-bump contract other ConfigService tests rely on
   (test_config_centralization.py's TestInitializeRuntimeDb asserts a
   SINGLE version bump from the unrelated lifecycle_analysis_config
   backfill in that exact no-op scenario).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import LifecycleAnalysisConfig
from tests.unit.server.siem.backends import (  # noqa: F401  (real-PG fixtures)
    _pg_session_pool,
    pg_pool,
)

# Named seed/expected version constants (avoid unexplained magic numbers).
_SEED_VERSION_LEGACY_ROW = 5
_EXPECTED_VERSION_AFTER_SINGLE_LIFECYCLE_BACKFILL = _SEED_VERSION_LEGACY_ROW + 1
_SEED_VERSION_ALREADY_PROMOTED_ROW = 9


def _create_server_config_table(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config ("
            "config_key TEXT PRIMARY KEY DEFAULT 'runtime', "
            "config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT DEFAULT (datetime('now')), "
            "updated_by TEXT)"
        )
        conn.commit()
    finally:
        conn.close()


def _seed_runtime_row(db_path: Path, runtime: dict, version: int) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO server_config "
            "(config_key, config_json, version, updated_by) "
            "VALUES ('runtime', ?, ?, 'test')",
            (json.dumps(runtime), version),
        )
        conn.commit()
    finally:
        conn.close()


def _read_raw_runtime_json(db_path: Path) -> str:
    """Return the raw config_json TEXT column, unparsed."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT config_json FROM server_config WHERE config_key = 'runtime'"
        ).fetchone()
        assert row is not None
        return row[0]  # type: ignore[no-any-return]
    finally:
        conn.close()


def _read_runtime_row(db_path: Path) -> dict:
    return json.loads(_read_raw_runtime_json(db_path))  # type: ignore[no-any-return]


def _read_runtime_row_version(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT version FROM server_config WHERE config_key = 'runtime'"
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        conn.close()


class TestAliasLockDbBackedPromotionMigration:
    def test_pre_phase3_stored_blob_is_promoted_to_true(self, tmp_path):
        """Stored blob predates Phase 3 (explicit section, no marker) -> True.

        Also predates lifecycle_analysis_config (deliberately omitted from
        the seed) -- proves the promotion write survives the adjacent A7d
        backfill re-save rather than being clobbered by it.
        """
        server_dir = tmp_path / "server"
        server_dir.mkdir()
        db_path = tmp_path / "cidx_server.db"
        _create_server_config_table(db_path)
        _seed_runtime_row(
            db_path,
            {
                "service_display_name": "Staging",
                "alias_lock_config": {"db_backed_enabled": False},
            },
            version=_SEED_VERSION_LEGACY_ROW,
        )

        svc = ConfigService(server_dir_path=str(server_dir))
        svc.load_config()
        svc.initialize_runtime_db(str(db_path))

        config = svc.get_config()
        assert config.alias_lock_config.db_backed_enabled is True
        assert config.alias_lock_config.db_backed_enabled_promoted is True

        # Durability: persisted to the DB row, not just in-memory.
        persisted = _read_runtime_row(db_path)
        assert persisted["alias_lock_config"]["db_backed_enabled"] is True
        assert persisted["alias_lock_config"]["db_backed_enabled_promoted"] is True

        # Idempotent across a simulated restart: a fresh ConfigService
        # reading the now-promoted row must not need to touch it again,
        # and must report the same promoted state.
        version_after_first_boot = _read_runtime_row_version(db_path)

        svc2 = ConfigService(server_dir_path=str(server_dir))
        svc2.load_config()
        svc2.initialize_runtime_db(str(db_path))
        assert svc2.get_config().alias_lock_config.db_backed_enabled is True

        version_after_second_boot = _read_runtime_row_version(db_path)
        assert version_after_second_boot == version_after_first_boot

    def test_operator_explicit_false_after_promotion_is_never_overwritten(
        self, tmp_path
    ):
        """Marker already True (promoted once, operator then chose False) ->
        stays False forever, AND the row is never rewritten at all: the DB
        version and the raw JSON string are byte-identical before/after
        every reload. lifecycle_analysis_config is present in the seed so
        the unrelated A7d backfill never fires here, keeping this "zero
        writes when already promoted" invariant unambiguous.
        """
        server_dir = tmp_path / "server"
        server_dir.mkdir()
        db_path = tmp_path / "cidx_server.db"
        _create_server_config_table(db_path)
        seed = {
            "service_display_name": "Staging",
            "lifecycle_analysis_config": asdict(LifecycleAnalysisConfig()),
            "alias_lock_config": {
                "db_backed_enabled": False,
                "db_backed_enabled_promoted": True,
            },
        }
        _seed_runtime_row(db_path, seed, version=_SEED_VERSION_ALREADY_PROMOTED_ROW)
        version_before = _read_runtime_row_version(db_path)
        raw_json_before = _read_raw_runtime_json(db_path)

        svc = ConfigService(server_dir_path=str(server_dir))
        svc.load_config()
        svc.initialize_runtime_db(str(db_path))

        config = svc.get_config()
        assert config.alias_lock_config.db_backed_enabled is False
        assert config.alias_lock_config.db_backed_enabled_promoted is True

        # No write at all should have happened: version and the raw JSON
        # string are byte-identical to what was seeded.
        assert _read_runtime_row_version(db_path) == version_before
        assert _read_raw_runtime_json(db_path) == raw_json_before

        # A second independent "restart" must also leave it untouched.
        svc2 = ConfigService(server_dir_path=str(server_dir))
        svc2.load_config()
        svc2.initialize_runtime_db(str(db_path))
        assert svc2.get_config().alias_lock_config.db_backed_enabled is False
        assert _read_runtime_row_version(db_path) == version_before
        assert _read_raw_runtime_json(db_path) == raw_json_before

    def test_absent_alias_lock_section_needs_no_promotion_write(self, tmp_path):
        """No alias_lock_config section at all -> already-correct default,
        zero extra version bump (only the unrelated lifecycle_analysis_config
        A7d backfill bumps the version once, matching the pre-existing
        contract test_config_centralization.py relies on)."""
        server_dir = tmp_path / "server"
        server_dir.mkdir()
        db_path = tmp_path / "cidx_server.db"
        _create_server_config_table(db_path)
        _seed_runtime_row(
            db_path,
            {
                "service_display_name": "FromSQLite",
                "jwt_expiration_minutes": 77,
            },
            version=_SEED_VERSION_LEGACY_ROW,
        )

        svc = ConfigService(server_dir_path=str(server_dir))
        svc.load_config()
        svc.initialize_runtime_db(str(db_path))

        config = svc.get_config()
        assert config.alias_lock_config.db_backed_enabled is True

        assert (
            svc._db_config_version == _EXPECTED_VERSION_AFTER_SINGLE_LIFECYCLE_BACKFILL
        )


def _pg_service(tmp_path: Path, pool: Any) -> ConfigService:
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    svc = ConfigService(server_dir_path=str(server_dir))
    svc.load_config()
    svc.set_connection_pool(pool)  # seeds the committed row
    return svc


def _make_pre_phase3_pg_row(pool: Any) -> None:  # Any: the pg_pool fixture's pool
    """The committed alias-lock section as a pre-Phase-3 release left it."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE server_config SET config_json = jsonb_set(config_json, "
            "'{alias_lock_config}', '{\"db_backed_enabled\": false}'::jsonb) "
            "WHERE config_key = 'runtime'"
        )


class TestAliasLockDbBackedPromotionMigrationPgBackend:
    """Requirement 1 ("must work for BOTH backends"): the PostgreSQL/cluster
    branch, against a REAL PostgreSQL (skipped without TEST_POSTGRES_DSN).

    Bug #2017: the promotion re-decides on the RE-READ committed row and
    writes it with a compare-and-set, so the assertions are on the committed
    row and the process cache, not on the object passed in."""

    def test_pre_phase3_row_is_promoted_in_the_committed_row(
        self,
        tmp_path: Path,
        pg_pool: Any,  # noqa: F811  (the imported fixture)
    ) -> None:
        svc = _pg_service(tmp_path, pg_pool)
        _make_pre_phase3_pg_row(pg_pool)
        version, raw = svc._read_committed_runtime()

        svc._apply_alias_lock_db_backed_promotion(svc.get_config(), raw)

        after, section = svc.read_committed_section("alias_lock_config")
        assert after == version + 1
        assert section["db_backed_enabled"] is True
        assert section["db_backed_enabled_promoted"] is True
        cached = svc.get_config().alias_lock_config
        assert cached.db_backed_enabled is True
        assert cached.db_backed_enabled_promoted is True

    def test_already_promoted_row_is_never_rewritten(
        self,
        tmp_path: Path,
        pg_pool: Any,  # noqa: F811  (the imported fixture)
    ) -> None:
        svc = _pg_service(tmp_path, pg_pool)
        _make_pre_phase3_pg_row(pg_pool)
        _version, raw = svc._read_committed_runtime()
        svc._apply_alias_lock_db_backed_promotion(svc.get_config(), raw)
        promoted_version, promoted_raw = svc._read_committed_runtime()

        # the real merge supplies the (now marked) committed row on reload
        svc._apply_alias_lock_db_backed_promotion(svc.get_config(), promoted_raw)

        assert svc._read_committed_runtime() == (promoted_version, promoted_raw)
