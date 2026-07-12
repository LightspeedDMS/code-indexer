"""Bug #1368: HNSWOrphanRepairSweepScheduler config read fails on cluster
PostgreSQL: 'dict' object has no attribute 'enabled' / 'batch_size'.

Root cause: ConfigService._merge_runtime_config() always round-trips the
full ServerConfig through dataclasses.asdict() (which recursively converts
EVERY nested dataclass field, including hnsw_orphan_repair_sweep_config,
into a plain dict) and then reconstructs via
ServerConfigManager._dict_to_server_config(). That reconstruction method has
an explicit dict -> dataclass conversion block for every other nested
config section (data_retention_config, activated_reaper_config, etc.) but
was MISSING one for hnsw_orphan_repair_sweep_config, so the field survived
the round-trip as a raw dict, and any `cfg.enabled` / `cfg.batch_size`
attribute access (exactly what scheduler.py's _loop()/_batch_size() do)
raised AttributeError -- silently caught by the scheduler's defensive
except-Exception fallback on EVERY cluster AND solo deployment (both
storage backends route through the identical _merge_runtime_config code
path: _load_runtime_from_pg -> _merge_runtime_config, and
_load_runtime_from_sqlite -> _merge_runtime_config).

These tests deliberately do NOT hand-construct an HNSWOrphanRepairSweepConfig
and inject it directly into a ServerConfig/ConfigService in-process -- that
is exactly the unfaithful-mock gap (see project memory
feedback_faithful_db_mocks.md) that let this regression through
undetected in the pre-existing test_hnsw_orphan_repair_sweep_config_1360.py
suite. Instead they drive the REAL ConfigService through its actual
seed-to-DB-then-reload-from-DB lifecycle (SQLite solo mode, and PostgreSQL
cluster mode gated on TEST_POSTGRES_DSN), proving the exact JSON round trip
a real server goes through on every startup.
"""

from __future__ import annotations

import os
import sqlite3

import pytest


def _make_sqlite_runtime_table(db_path: str) -> None:
    """Create the server_config table with the real production DDL
    (mirrors DatabaseManager.CREATE_SERVER_CONFIG_TABLE) so
    initialize_runtime_db() has somewhere to seed/read from -- a real
    server always has this table created by DatabaseManager at startup
    before ConfigService.initialize_runtime_db() runs."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config ("
            "config_key TEXT PRIMARY KEY DEFAULT 'runtime', "
            "config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT DEFAULT (datetime('now')), "
            "updated_by TEXT)"
        )
        conn.commit()


# ---------------------------------------------------------------------------
# SQLite runtime-DB round trip (solo mode) -- always runs, no external deps.
#
# Bug #1368's live evidence was captured on a PostgreSQL cluster, but
# _merge_runtime_config / _dict_to_server_config is the SAME function for
# both backends (ConfigService.load_config() routes SQLite runtime through
# _merge_runtime_config exactly like _load_runtime_from_pg does for
# PostgreSQL), so the SQLite path reproduces the identical regression and
# proves the fix without requiring a live PostgreSQL instance.
# ---------------------------------------------------------------------------


class TestConfigServiceSqliteRoundTrip:
    """Real ConfigService + real SQLite runtime DB (no mocks, no
    hand-constructed HNSWOrphanRepairSweepConfig)."""

    def test_fresh_server_first_boot_seed_produces_typed_config(self, tmp_path):
        """First-boot seeding (server_config table empty -> seed from
        bootstrap defaults) must leave hnsw_orphan_repair_sweep_config as a
        real HNSWOrphanRepairSweepConfig on the SAME ConfigService instance
        that performed the seed -- this is the in-process object, not yet a
        DB round trip, and is expected to already be typed via __post_init__.
        """
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        db_path = str(tmp_path / "runtime.db")
        _make_sqlite_runtime_table(db_path)
        service.initialize_runtime_db(db_path)

        cfg = service.get_config().hnsw_orphan_repair_sweep_config
        assert bool(cfg.enabled) is True
        assert int(cfg.batch_size) == 15
        assert int(cfg.tick_interval_minutes) == 7

    def test_server_restart_reload_from_sqlite_produces_typed_config(self, tmp_path):
        """The REAL regression: after a first-boot seed writes the runtime
        JSON blob to SQLite (exactly as a real server does), a FRESH
        ConfigService instance simulating a server restart / new process
        reloads that JSON blob back via _merge_runtime_config ->
        _dict_to_server_config. Before the fix, hnsw_orphan_repair_sweep_config
        came back as a plain dict and `cfg.enabled` raised AttributeError --
        exactly the staging WARNING log lines from bug #1368. This exercises
        the scheduler's EXACT attribute-access pattern
        (scheduler.py _batch_size()/_loop()): bool(cfg.enabled),
        int(cfg.batch_size), int(cfg.tick_interval_minutes).
        """
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import (
            HNSWOrphanRepairSweepConfig,
        )

        db_path = str(tmp_path / "runtime.db")
        _make_sqlite_runtime_table(db_path)

        # First boot: seeds the SQLite runtime table from bootstrap defaults.
        boot_service = ConfigService(server_dir_path=str(tmp_path))
        boot_service.initialize_runtime_db(db_path)

        # Simulate a server restart: brand-new ConfigService instance,
        # same server_dir + same SQLite runtime DB path.
        restarted_service = ConfigService(server_dir_path=str(tmp_path))
        restarted_service.initialize_runtime_db(db_path)

        cfg = restarted_service.get_config().hnsw_orphan_repair_sweep_config

        assert isinstance(cfg, HNSWOrphanRepairSweepConfig), (
            f"Expected HNSWOrphanRepairSweepConfig, got {type(cfg).__name__} "
            f"({cfg!r}) -- Bug #1368: 'dict' object has no attribute 'enabled'"
        )
        # Mirrors scheduler.py's exact attribute-access + cast pattern.
        assert bool(cfg.enabled) is True
        assert int(cfg.batch_size) == 15
        assert int(cfg.tick_interval_minutes) == 7

    def test_custom_web_ui_values_survive_restart_reload(self, tmp_path):
        """AC4 regression proof: a Web-UI-configured non-default value must
        both PERSIST and remain correctly TYPED after a restart -- proving
        the fix restores real runtime configurability, not just prevents
        the AttributeError.
        """
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import (
            HNSWOrphanRepairSweepConfig,
        )
        from dataclasses import asdict

        db_path = str(tmp_path / "runtime.db")
        _make_sqlite_runtime_table(db_path)

        boot_service = ConfigService(server_dir_path=str(tmp_path))
        boot_service.initialize_runtime_db(db_path)

        # Simulate a Web-UI settings change: mutate the in-memory config and
        # save it, exactly like ConfigService.save_config() does for any
        # other runtime setting.
        config = boot_service.get_config()
        config.hnsw_orphan_repair_sweep_config = HNSWOrphanRepairSweepConfig(
            enabled=False, batch_size=99, tick_interval_minutes=42
        )
        boot_service.save_config(config)

        # Simulate a server restart.
        restarted_service = ConfigService(server_dir_path=str(tmp_path))
        restarted_service.initialize_runtime_db(db_path)
        cfg = restarted_service.get_config().hnsw_orphan_repair_sweep_config

        assert isinstance(cfg, HNSWOrphanRepairSweepConfig), (
            f"Expected HNSWOrphanRepairSweepConfig, got {type(cfg).__name__} ({cfg!r})"
        )
        assert bool(cfg.enabled) is False
        assert int(cfg.batch_size) == 99
        assert int(cfg.tick_interval_minutes) == 42
        # Sanity: the persisted dict actually carried the custom values
        # (proves this is a real DB round trip, not an in-memory artifact).
        assert asdict(cfg) == {
            "enabled": False,
            "batch_size": 99,
            "tick_interval_minutes": 42,
        }


# ---------------------------------------------------------------------------
# Live PostgreSQL round trip (cluster mode) -- gated on TEST_POSTGRES_DSN,
# same convention as test_token_bucket_pg_atomic_concurrency_1334.py /
# test_per_consumer_rate_limiter_live_pg_1332.py / test_migration_runner.py.
# Skipped automatically when no real PostgreSQL is reachable.
# ---------------------------------------------------------------------------

HAS_PSYCOPG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG = True
except ImportError:
    pass


@pytest.fixture(scope="module")
def pg_dsn():
    """Module-scoped DSN string for live-PG tests. Skips if unavailable."""
    if not HAS_PSYCOPG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        import psycopg

        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    return dsn


@pytest.fixture
def isolated_server_config_table(pg_dsn):
    """Fresh server_config table for each test, dropped after -- uses the
    EXACT production DDL from migrations/sql/010_server_config.sql so the
    test proves behavior against the real cluster schema, not an ad hoc
    approximation."""
    import psycopg

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS server_config")
        conn.execute(
            """
            CREATE TABLE server_config (
                config_key TEXT PRIMARY KEY DEFAULT 'runtime',
                config_json JSONB NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_by TEXT
            )
            """
        )
    yield
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS server_config")


@pytest.mark.skipif(not HAS_PSYCOPG, reason="psycopg not available")
class TestConfigServiceLivePostgresRoundTrip:
    """Real ConfigService against a REAL PostgreSQL server_config table
    (the exact production schema/backend where bug #1368 was discovered
    live on staging) -- no mocks, no hand-constructed dataclass injection.
    """

    def test_cluster_first_boot_seed_then_second_node_reload_produces_typed_config(
        self, pg_dsn, isolated_server_config_table, tmp_path
    ):
        """Simulates the real cluster sequence: node A boots first and
        seeds server_config (asdict() flattens hnsw_orphan_repair_sweep_config
        to a plain dict in the JSONB column, exactly as production does).
        Node B (a fresh ConfigService + fresh pool, simulating a second
        cluster node or a restart) then loads that same JSONB row via
        _load_runtime_from_pg -> _merge_runtime_config ->
        _dict_to_server_config. Before the fix this reproduced the exact
        staging WARNING: "'dict' object has no attribute 'enabled'".
        """
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.utils.config_manager import (
            HNSWOrphanRepairSweepConfig,
        )

        # Node A: first boot, seeds server_config from bootstrap defaults.
        pool_a = ConnectionPool(pg_dsn, min_size=1, max_size=2, name="node-a")
        node_a = ConfigService(server_dir_path=str(tmp_path / "node_a"))
        node_a.set_connection_pool(pool_a)

        # Node B: independent ConfigService + independent pool (mirrors a
        # separate uvicorn worker/cluster node), reloading the SAME PG row.
        pool_b = ConnectionPool(pg_dsn, min_size=1, max_size=2, name="node-b")
        node_b = ConfigService(server_dir_path=str(tmp_path / "node_b"))
        node_b.set_connection_pool(pool_b)

        cfg = node_b.get_config().hnsw_orphan_repair_sweep_config

        assert isinstance(cfg, HNSWOrphanRepairSweepConfig), (
            f"Expected HNSWOrphanRepairSweepConfig, got {type(cfg).__name__} "
            f"({cfg!r}) -- Bug #1368 live staging failure mode: "
            "'dict' object has no attribute 'enabled'"
        )
        # Mirrors scheduler.py's exact attribute-access + cast pattern.
        assert bool(cfg.enabled) is True
        assert int(cfg.batch_size) == 15
        assert int(cfg.tick_interval_minutes) == 7

    def test_cluster_web_ui_change_survives_cross_node_reload(
        self, pg_dsn, isolated_server_config_table, tmp_path
    ):
        """AC4 proof under real PostgreSQL: a Web-UI-configured
        non-default value written by one node must be visible, correctly
        typed, and correctly valued when read back by another node --
        proving cross-node runtime configurability actually works, not
        just that the AttributeError is silenced.
        """
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.utils.config_manager import (
            HNSWOrphanRepairSweepConfig,
        )

        pool_a = ConnectionPool(pg_dsn, min_size=1, max_size=2, name="node-a")
        node_a = ConfigService(server_dir_path=str(tmp_path / "node_a"))
        node_a.set_connection_pool(pool_a)

        config = node_a.get_config()
        config.hnsw_orphan_repair_sweep_config = HNSWOrphanRepairSweepConfig(
            enabled=False, batch_size=3, tick_interval_minutes=11
        )
        node_a.save_config(config)

        pool_b = ConnectionPool(pg_dsn, min_size=1, max_size=2, name="node-b")
        node_b = ConfigService(server_dir_path=str(tmp_path / "node_b"))
        node_b.set_connection_pool(pool_b)

        cfg = node_b.get_config().hnsw_orphan_repair_sweep_config

        assert isinstance(cfg, HNSWOrphanRepairSweepConfig)
        assert bool(cfg.enabled) is False
        assert int(cfg.batch_size) == 3
        assert int(cfg.tick_interval_minutes) == 11
