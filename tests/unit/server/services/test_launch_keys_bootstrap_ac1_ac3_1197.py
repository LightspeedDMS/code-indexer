"""Story #1197 AC1 + AC3: Bootstrap key removal and transition strip guard.

RED-phase tests — all must FAIL before production code is written.

AC1: host/port/workers/log_level removed from BOOTSTRAP_KEYS.
AC3: _strip_config_file_to_bootstrap preserves them via TRANSITION_PRESERVE_KEYS.
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


class TestAC3TransitionPreserveKeys:
    """Story #1197 AC3: TRANSITION_PRESERVE_KEYS constant and strip guard."""

    def test_transition_preserve_keys_constant_exists(self) -> None:
        from code_indexer.server.services.config_service import TRANSITION_PRESERVE_KEYS

        assert isinstance(TRANSITION_PRESERVE_KEYS, (set, frozenset))

    def test_transition_preserve_keys_contains_four_launch_keys(self) -> None:
        from code_indexer.server.services.config_service import TRANSITION_PRESERVE_KEYS

        assert "workers" in TRANSITION_PRESERVE_KEYS
        assert "log_level" in TRANSITION_PRESERVE_KEYS
        assert "host" in TRANSITION_PRESERVE_KEYS
        assert "port" in TRANSITION_PRESERVE_KEYS

    def test_transition_preserve_keys_exactly_four(self) -> None:
        from code_indexer.server.services.config_service import TRANSITION_PRESERVE_KEYS

        assert len(TRANSITION_PRESERVE_KEYS) == 4, (
            f"TRANSITION_PRESERVE_KEYS must have exactly 4 keys, got {len(TRANSITION_PRESERVE_KEYS)}"
        )

    @pytest.mark.slow
    def test_strip_preserves_four_launch_keys(self, tmp_path: Path) -> None:
        """After initialize_runtime_db, config.json STILL has the four keys."""
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
        assert "workers" in saved, (
            "AC3: workers must survive strip (transition allow-list)"
        )
        assert "log_level" in saved, "AC3: log_level must survive strip"
        assert "host" in saved, "AC3: host must survive strip"
        assert "port" in saved, "AC3: port must survive strip"

    @pytest.mark.slow
    def test_strip_still_removes_non_transition_runtime_keys(
        self, tmp_path: Path
    ) -> None:
        """The allow-list narrows strip behavior but does NOT disable it."""
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
            "AC3: non-launch runtime keys must still be stripped (allow-list narrows, not disables)"
        )
