"""Story #1197 AC1: Bootstrap key removal.  Story #1196: transition cleanup.

AC1 (Story #1197): host/port/workers/log_level removed from BOOTSTRAP_KEYS.
Story #1196 (next-release cleanup) removes the Story #1197 AC3/AC6 transition
mechanism entirely -- TRANSITION_PRESERVE_KEYS is gone, _strip_config_file_to_bootstrap()
strips the four keys again, and save_config() no longer writes them to config.json.
"""

import json
import sqlite3
from pathlib import Path

import pytest


def _make_sqlite_db(db_path: str) -> None:
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


class TestAC1BootstrapKeysRemoved:
    """Story #1197 AC1: four launch keys must NOT be in BOOTSTRAP_KEYS."""

    def test_workers_not_in_bootstrap_keys(self) -> None:
        from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

        assert "workers" not in BOOTSTRAP_KEYS, (
            "Story #1197 AC1: 'workers' must be removed from BOOTSTRAP_KEYS"
        )

    def test_log_level_not_in_bootstrap_keys(self) -> None:
        from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

        assert "log_level" not in BOOTSTRAP_KEYS, (
            "Story #1197 AC1: 'log_level' must be removed from BOOTSTRAP_KEYS"
        )

    def test_host_not_in_bootstrap_keys(self) -> None:
        from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

        assert "host" not in BOOTSTRAP_KEYS, (
            "Story #1197 AC1: 'host' must be removed from BOOTSTRAP_KEYS"
        )

    def test_port_not_in_bootstrap_keys(self) -> None:
        from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

        assert "port" not in BOOTSTRAP_KEYS, (
            "Story #1197 AC1: 'port' must be removed from BOOTSTRAP_KEYS"
        )

    def test_all_other_bootstrap_keys_undisturbed(self) -> None:
        """Regression: ALL non-launch bootstrap keys must remain after AC1 change."""
        from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

        EXPECTED_REMAINING = frozenset(
            {
                "server_dir",
                "storage_mode",
                "postgres_dsn",
                "ontap",
                "cluster",
                "enable_malloc_arena_max",
                "enable_malloc_trim",
                "enable_graph_channel_repair",
                "graph_repair_self_loop",
                "graph_repair_malformed_yaml",
                "graph_repair_garbage_domain",
                "graph_repair_bidirectional_mismatch",
                "fault_injection_enabled",
                "fault_injection_nonprod_ack",
                "server_threadpool_size",
                "pace_maker_clone_path",
                "mcp_dispatch_pool_size",
                "query_executor_pool_size",
                "enable_predeactivation_leak_scan",
                "orphan_trash_sweep_per_startup_cap",
                "clone_backend",
                "cow_daemon",
            }
        )

        missing = EXPECTED_REMAINING - BOOTSTRAP_KEYS
        assert not missing, (
            f"Story #1197 AC1 regression: accidentally removed bootstrap keys: {missing}. "
            "Only host/port/workers/log_level should be removed."
        )

    def test_partition_audit_four_keys_now_runtime(self) -> None:
        """After AC1, all ServerConfig fields must still be classified (partition complete)."""
        import dataclasses
        from code_indexer.server.services.config_service import BOOTSTRAP_KEYS
        from code_indexer.server.utils.config_manager import ServerConfig

        # The four launch keys move from BOOTSTRAP_KEYS to this runtime set
        KNOWN_RUNTIME_KEYS = frozenset(
            {
                "jwt_expiration_minutes",
                "service_display_name",
                "password_security",
                "resource_config",
                "cache_config",
                "oidc_provider_config",
                "telemetry_config",
                "search_limits_config",
                "golden_repos_config",
                "mcp_session_config",
                "health_config",
                "scip_config",
                "git_timeouts_config",
                "error_handling_config",
                "api_limits_config",
                "web_security_config",
                "indexing_config",
                "claude_integration_config",
                "repository_config",
                "multi_search_limits_config",
                "background_jobs_config",
                "content_limits_config",
                "self_monitoring_config",
                "langfuse_config",
                "mcp_self_registration",
                "wiki_config",
                "data_retention_config",
                "password_expiry_config",
                "rerank_config",
                "voyage_ai_sinbin",
                "cohere_sinbin",
                "query_orchestration",
                "memory_retrieval_config",
                "lifecycle_analysis_config",
                "codex_integration_config",
                "elevation_enforcement_enabled",
                "elevation_idle_timeout_seconds",
                "elevation_max_age_seconds",
                "cidx_meta_backup_config",
                "activated_reaper_config",
                "xray_config",
                "pace_maker_mode",
                "query_provider_max_concurrency",
                "coalesce_enabled",
                "coalesce_max_batch_size",
                "coalesce_k_min",
                "coalesce_k_max",
                "snapshot_retention_keep_last",
                "nfs_visibility_timeout_seconds",
                "research_session_retention_days",
                "query_embedding_cache_config",
                "search_event_log_retention_days",
                "export_retention_days",
                # Story #1197: the four launch keys are now runtime
                "workers",
                "log_level",
                "host",
                "port",
                # PR #1332: admission control is opt-in (both gates default
                # False) and read from the merged config at app-wiring time,
                # not needed pre-DB -- runtime, not bootstrap.
                "admission_control_config",
            }
        )

        all_fields = {f.name for f in dataclasses.fields(ServerConfig)}
        classified = BOOTSTRAP_KEYS | KNOWN_RUNTIME_KEYS

        unclassified = all_fields - classified
        assert not unclassified, (
            f"Unclassified ServerConfig fields after Story #1197: {unclassified}. "
            "Add to BOOTSTRAP_KEYS or runtime set."
        )
        overlap = BOOTSTRAP_KEYS & KNOWN_RUNTIME_KEYS
        assert not overlap, f"Fields in both sets (must be disjoint): {overlap}"


class TestStory1196TransitionMechanismRemoved:
    """Story #1196 AC1: the Story #1197 transition allow-list is fully removed.

    Story #1197 kept host/port/workers/log_level in config.json for one release
    (via TRANSITION_PRESERVE_KEYS) so old nodes in a rolling upgrade could still
    read them.  Story #1196 is the next-release cleanup: the operator has
    confirmed all cluster nodes are on the new release, so both the strip-guard
    and the write-path inclusion are removed, and the four keys disappear from
    config.json entirely.
    """

    def test_transition_preserve_keys_constant_removed(self) -> None:
        """AC1 (MAJOR-5): TRANSITION_PRESERVE_KEYS must no longer exist."""
        import code_indexer.server.services.config_service as config_service_module

        assert not hasattr(config_service_module, "TRANSITION_PRESERVE_KEYS"), (
            "Story #1196 AC1: TRANSITION_PRESERVE_KEYS must be removed from "
            "config_service.py -- the transition window has closed."
        )

    @pytest.mark.slow
    def test_strip_removes_four_launch_keys_after_cleanup(self, tmp_path: Path) -> None:
        """AC1: after initialize_runtime_db, config.json no longer has the four keys."""
        from code_indexer.server.services.config_service import ConfigService

        config_file = tmp_path / "config.json"
        initial_config = {
            "server_dir": str(tmp_path),
            "host": "0.0.0.0",
            "port": 9000,
            "workers": 3,
            "log_level": "WARNING",
            "storage_mode": "local",
            "jwt_expiration_minutes": 999,  # pure runtime key — will be stripped
        }
        config_file.write_text(json.dumps(initial_config))

        db_path = str(tmp_path / "cidx_server.db")
        _make_sqlite_db(db_path)

        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.load_config()
        svc.initialize_runtime_db(db_path)

        saved = json.loads(config_file.read_text())
        assert "workers" not in saved, (
            "AC1: workers must NOT survive the strip after transition cleanup"
        )
        assert "log_level" not in saved, "AC1: log_level must NOT survive the strip"
        assert "host" not in saved, "AC1: host must NOT survive the strip"
        assert "port" not in saved, "AC1: port must NOT survive the strip"

    @pytest.mark.slow
    def test_strip_still_removes_non_bootstrap_runtime_keys(
        self, tmp_path: Path
    ) -> None:
        """Regression: unrelated runtime keys are still stripped as before."""
        from code_indexer.server.services.config_service import ConfigService

        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "server_dir": str(tmp_path),
                    "storage_mode": "local",
                    "host": "127.0.0.1",
                    "port": 8000,
                    "workers": 1,
                    "log_level": "INFO",
                    "jwt_expiration_minutes": 999,  # must be stripped
                }
            )
        )

        db_path = str(tmp_path / "cidx_server.db")
        _make_sqlite_db(db_path)

        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.load_config()
        svc.initialize_runtime_db(db_path)

        saved = json.loads(config_file.read_text())
        assert "jwt_expiration_minutes" not in saved, (
            "Non-launch runtime keys must still be stripped"
        )

    @pytest.mark.slow
    def test_bootstrap_keys_remain_after_strip(self, tmp_path: Path) -> None:
        """AC1: bootstrap-only keys (server_dir, storage_mode, ...) remain in config.json."""
        from code_indexer.server.services.config_service import ConfigService

        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "server_dir": str(tmp_path),
                    "storage_mode": "local",
                    "host": "127.0.0.1",
                    "port": 8000,
                    "workers": 1,
                    "log_level": "INFO",
                }
            )
        )

        db_path = str(tmp_path / "cidx_server.db")
        _make_sqlite_db(db_path)

        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.load_config()
        svc.initialize_runtime_db(db_path)

        saved = json.loads(config_file.read_text())
        assert saved.get("server_dir") == str(tmp_path), (
            "AC1: server_dir (bootstrap) must remain in config.json"
        )
        assert saved.get("storage_mode") == "local", (
            "AC1: storage_mode (bootstrap) must remain in config.json"
        )

    @pytest.mark.slow
    def test_save_config_no_longer_writes_four_launch_keys(
        self, tmp_path: Path
    ) -> None:
        """AC1 (MAJOR-5): a normal save_config() no longer writes the four keys.

        Story #1197 AC6 added _extract_bootstrap_dict_with_transition() so every
        settings-save re-wrote the four keys into config.json.  Story #1196
        removes that write-path inclusion: a subsequent save must write only
        bootstrap keys.
        """
        from code_indexer.server.services.config_service import ConfigService

        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "server_dir": str(tmp_path),
                    "storage_mode": "local",
                    "host": "127.0.0.1",
                    "port": 8000,
                    "workers": 1,
                    "log_level": "INFO",
                }
            )
        )

        db_path = str(tmp_path / "cidx_server.db")
        _make_sqlite_db(db_path)

        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.load_config()
        svc.initialize_runtime_db(db_path)

        # A subsequent normal settings-save (e.g. an unrelated field change).
        config = svc.get_config()
        config.workers = 4  # in-memory TARGET change
        svc.save_config(config)

        saved = json.loads(config_file.read_text())
        assert "workers" not in saved, (
            "AC1: save_config() must NOT write 'workers' to config.json anymore"
        )
        assert "log_level" not in saved, (
            "AC1: save_config() must NOT write 'log_level' to config.json anymore"
        )
        assert "host" not in saved, (
            "AC1: save_config() must NOT write 'host' to config.json anymore"
        )
        assert "port" not in saved, (
            "AC1: save_config() must NOT write 'port' to config.json anymore"
        )
