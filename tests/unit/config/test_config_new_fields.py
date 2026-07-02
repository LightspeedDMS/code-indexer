"""Tests for Bug #678: new config fields on VoyageAIConfig, CohereConfig, and server runtime.

Tests: HealthMonitorConfig, reranker_timeout/connect_timeout fields, ProviderSinBinConfig,
QueryOrchestrationConfig, and ServerConfig integration.
"""

from dataclasses import fields as dc_fields


class TestHealthMonitorConfig:
    """HealthMonitorConfig nested Pydantic model — shared structure per provider."""

    def test_default_rolling_window_minutes(self):
        from code_indexer.config import HealthMonitorConfig

        cfg = HealthMonitorConfig()
        assert cfg.rolling_window_minutes == 60

    def test_default_down_consecutive_failures(self):
        from code_indexer.config import HealthMonitorConfig

        cfg = HealthMonitorConfig()
        assert cfg.down_consecutive_failures == 5

    def test_default_down_error_rate(self):
        from code_indexer.config import HealthMonitorConfig

        cfg = HealthMonitorConfig()
        assert cfg.down_error_rate == 0.5

    def test_default_degraded_error_rate(self):
        from code_indexer.config import HealthMonitorConfig

        cfg = HealthMonitorConfig()
        assert cfg.degraded_error_rate == 0.1

    def test_default_latency_p95_threshold_ms(self):
        from code_indexer.config import HealthMonitorConfig

        cfg = HealthMonitorConfig()
        assert cfg.latency_p95_threshold_ms == 5000.0

    def test_default_availability_threshold(self):
        from code_indexer.config import HealthMonitorConfig

        cfg = HealthMonitorConfig()
        assert cfg.availability_threshold == 0.95

    def test_custom_values_accepted(self):
        from code_indexer.config import HealthMonitorConfig

        cfg = HealthMonitorConfig(
            rolling_window_minutes=30,
            down_consecutive_failures=3,
            down_error_rate=0.7,
            degraded_error_rate=0.2,
            latency_p95_threshold_ms=3000.0,
            availability_threshold=0.99,
        )
        assert cfg.rolling_window_minutes == 30
        assert cfg.down_consecutive_failures == 3
        assert cfg.down_error_rate == 0.7
        assert cfg.degraded_error_rate == 0.2
        assert cfg.latency_p95_threshold_ms == 3000.0
        assert cfg.availability_threshold == 0.99

    def test_is_pydantic_model(self):
        from code_indexer.config import HealthMonitorConfig
        from pydantic import BaseModel

        assert issubclass(HealthMonitorConfig, BaseModel)


class TestVoyageAIConfigNewFields:
    """VoyageAIConfig gains reranker_timeout, reranker_connect_timeout, health_monitor."""

    def test_reranker_timeout_default(self):
        from code_indexer.config import VoyageAIConfig

        cfg = VoyageAIConfig()
        assert cfg.reranker_timeout == 15.0

    def test_reranker_connect_timeout_default(self):
        from code_indexer.config import VoyageAIConfig

        cfg = VoyageAIConfig()
        assert cfg.reranker_connect_timeout == 5.0

    def test_health_monitor_default_is_health_monitor_config(self):
        from code_indexer.config import VoyageAIConfig, HealthMonitorConfig

        cfg = VoyageAIConfig()
        assert isinstance(cfg.health_monitor, HealthMonitorConfig)

    def test_health_monitor_default_values(self):
        from code_indexer.config import VoyageAIConfig

        cfg = VoyageAIConfig()
        assert cfg.health_monitor.rolling_window_minutes == 60
        assert cfg.health_monitor.down_consecutive_failures == 5

    def test_custom_reranker_timeout(self):
        from code_indexer.config import VoyageAIConfig

        cfg = VoyageAIConfig(reranker_timeout=30.0)
        assert cfg.reranker_timeout == 30.0

    def test_custom_reranker_connect_timeout(self):
        from code_indexer.config import VoyageAIConfig

        cfg = VoyageAIConfig(reranker_connect_timeout=10.0)
        assert cfg.reranker_connect_timeout == 10.0

    def test_custom_health_monitor_nested(self):
        from code_indexer.config import VoyageAIConfig, HealthMonitorConfig

        hm = HealthMonitorConfig(rolling_window_minutes=30)
        cfg = VoyageAIConfig(health_monitor=hm)
        assert cfg.health_monitor.rolling_window_minutes == 30

    def test_existing_fields_unchanged(self):
        """Existing fields must not be broken by new additions."""
        from code_indexer.config import VoyageAIConfig

        cfg = VoyageAIConfig()
        assert cfg.timeout == 30
        assert cfg.connect_timeout == 5
        assert cfg.max_retries == 3
        assert cfg.parallel_requests == 8


class TestCohereConfigNewFields:
    """CohereConfig gains reranker_timeout, reranker_connect_timeout, health_monitor."""

    def test_reranker_timeout_default(self):
        from code_indexer.config import CohereConfig

        cfg = CohereConfig()
        assert cfg.reranker_timeout == 15.0

    def test_reranker_connect_timeout_default(self):
        from code_indexer.config import CohereConfig

        cfg = CohereConfig()
        assert cfg.reranker_connect_timeout == 5.0

    def test_health_monitor_default_is_health_monitor_config(self):
        from code_indexer.config import CohereConfig, HealthMonitorConfig

        cfg = CohereConfig()
        assert isinstance(cfg.health_monitor, HealthMonitorConfig)

    def test_health_monitor_mirrors_voyage_defaults(self):
        from code_indexer.config import CohereConfig

        cfg = CohereConfig()
        assert cfg.health_monitor.rolling_window_minutes == 60
        assert cfg.health_monitor.down_consecutive_failures == 5
        assert cfg.health_monitor.down_error_rate == 0.5
        assert cfg.health_monitor.availability_threshold == 0.95

    def test_custom_values_accepted(self):
        from code_indexer.config import CohereConfig

        cfg = CohereConfig(reranker_timeout=20.0, reranker_connect_timeout=8.0)
        assert cfg.reranker_timeout == 20.0
        assert cfg.reranker_connect_timeout == 8.0

    def test_existing_fields_unchanged(self):
        """Existing fields must not be broken by new additions."""
        from code_indexer.config import CohereConfig

        cfg = CohereConfig()
        assert cfg.timeout == 30
        assert cfg.connect_timeout == 5
        assert cfg.max_retries == 3


class TestProviderSinBinConfig:
    """ProviderSinBinConfig dataclass with sin-bin tunables."""

    def test_default_failure_threshold(self):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig()
        assert cfg.failure_threshold == 5

    def test_default_failure_window_seconds(self):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig()
        assert cfg.failure_window_seconds == 60

    def test_default_initial_cooldown_seconds(self):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig()
        assert cfg.initial_cooldown_seconds == 30

    def test_default_max_cooldown_seconds(self):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig()
        assert cfg.max_cooldown_seconds == 300

    def test_default_backoff_multiplier(self):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig()
        assert cfg.backoff_multiplier == 2.0

    def test_custom_values_accepted(self):
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        cfg = ProviderSinBinConfig(
            failure_threshold=3,
            failure_window_seconds=30,
            initial_cooldown_seconds=10,
            max_cooldown_seconds=120,
            backoff_multiplier=1.5,
        )
        assert cfg.failure_threshold == 3
        assert cfg.failure_window_seconds == 30
        assert cfg.initial_cooldown_seconds == 10
        assert cfg.max_cooldown_seconds == 120
        assert cfg.backoff_multiplier == 1.5


class TestQueryOrchestrationConfig:
    """QueryOrchestrationConfig dataclass with orchestration tunables."""

    def test_default_parallel_query_orchestrator_timeout_seconds(self):
        from code_indexer.server.utils.config_manager import QueryOrchestrationConfig

        cfg = QueryOrchestrationConfig()
        assert cfg.parallel_query_orchestrator_timeout_seconds == 20

    def test_default_max_query_latency_budget_seconds(self):
        from code_indexer.server.utils.config_manager import QueryOrchestrationConfig

        cfg = QueryOrchestrationConfig()
        assert cfg.max_query_latency_budget_seconds == 60

    def test_default_all_providers_sinbinned_retry_limit(self):
        from code_indexer.server.utils.config_manager import QueryOrchestrationConfig

        cfg = QueryOrchestrationConfig()
        assert cfg.all_providers_sinbinned_retry_limit == 2

    def test_default_provider_health_probe_interval_seconds(self):
        from code_indexer.server.utils.config_manager import QueryOrchestrationConfig

        cfg = QueryOrchestrationConfig()
        assert cfg.provider_health_probe_interval_seconds == 30

    def test_default_provider_health_probe_join_timeout_seconds(self):
        from code_indexer.server.utils.config_manager import QueryOrchestrationConfig

        cfg = QueryOrchestrationConfig()
        assert cfg.provider_health_probe_join_timeout_seconds == 5

    def test_custom_values_accepted(self):
        from code_indexer.server.utils.config_manager import QueryOrchestrationConfig

        cfg = QueryOrchestrationConfig(
            parallel_query_orchestrator_timeout_seconds=30,
            max_query_latency_budget_seconds=90,
            all_providers_sinbinned_retry_limit=3,
            provider_health_probe_interval_seconds=15,
            provider_health_probe_join_timeout_seconds=10,
        )
        assert cfg.parallel_query_orchestrator_timeout_seconds == 30
        assert cfg.max_query_latency_budget_seconds == 90
        assert cfg.all_providers_sinbinned_retry_limit == 3
        assert cfg.provider_health_probe_interval_seconds == 15
        assert cfg.provider_health_probe_join_timeout_seconds == 10


class TestServerConfigSinBinFields:
    """ServerConfig gains voyage_ai_sinbin, cohere_sinbin, query_orchestration fields."""

    def test_voyage_ai_sinbin_field_exists(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        field_names = {f.name for f in dc_fields(ServerConfig)}
        assert "voyage_ai_sinbin" in field_names

    def test_cohere_sinbin_field_exists(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        field_names = {f.name for f in dc_fields(ServerConfig)}
        assert "cohere_sinbin" in field_names

    def test_query_orchestration_field_exists(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        field_names = {f.name for f in dc_fields(ServerConfig)}
        assert "query_orchestration" in field_names

    def test_post_init_initializes_voyage_sinbin(self):
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            ProviderSinBinConfig,
        )

        cfg = ServerConfig(server_dir="/tmp/test")
        assert isinstance(cfg.voyage_ai_sinbin, ProviderSinBinConfig)

    def test_post_init_initializes_cohere_sinbin(self):
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            ProviderSinBinConfig,
        )

        cfg = ServerConfig(server_dir="/tmp/test")
        assert isinstance(cfg.cohere_sinbin, ProviderSinBinConfig)

    def test_post_init_initializes_query_orchestration(self):
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            QueryOrchestrationConfig,
        )

        cfg = ServerConfig(server_dir="/tmp/test")
        assert isinstance(cfg.query_orchestration, QueryOrchestrationConfig)

    def test_voyage_sinbin_defaults(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        cfg = ServerConfig(server_dir="/tmp/test")
        assert cfg.voyage_ai_sinbin.failure_threshold == 5
        assert cfg.voyage_ai_sinbin.initial_cooldown_seconds == 30
        assert cfg.voyage_ai_sinbin.max_cooldown_seconds == 300
        assert cfg.voyage_ai_sinbin.backoff_multiplier == 2.0

    def test_cohere_sinbin_mirrors_voyage_defaults(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        cfg = ServerConfig(server_dir="/tmp/test")
        assert cfg.cohere_sinbin.failure_threshold == 5
        assert cfg.cohere_sinbin.initial_cooldown_seconds == 30
        assert cfg.cohere_sinbin.max_cooldown_seconds == 300

    def test_query_orchestration_defaults(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        cfg = ServerConfig(server_dir="/tmp/test")
        assert cfg.query_orchestration.parallel_query_orchestrator_timeout_seconds == 20
        assert cfg.query_orchestration.max_query_latency_budget_seconds == 60
        assert cfg.query_orchestration.all_providers_sinbinned_retry_limit == 2

    def test_load_config_deserializes_voyage_sinbin_dict(self):
        """load_config must convert sinbin dicts to ProviderSinBinConfig."""
        import json
        import os
        import tempfile
        from code_indexer.server.utils.config_manager import (
            ServerConfigManager,
            ProviderSinBinConfig,
        )

        config_data = {
            "server_dir": "/tmp/test",
            "voyage_ai_sinbin": {
                "failure_threshold": 3,
                "failure_window_seconds": 30,
                "initial_cooldown_seconds": 15,
                "max_cooldown_seconds": 180,
                "backoff_multiplier": 1.5,
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = os.path.join(tmpdir, "config.json")
            with open(config_file, "w") as f:
                json.dump(config_data, f)
            manager = ServerConfigManager(server_dir_path=tmpdir)
            cfg = manager.load_config()
        assert isinstance(cfg.voyage_ai_sinbin, ProviderSinBinConfig)
        assert cfg.voyage_ai_sinbin.failure_threshold == 3
        assert cfg.voyage_ai_sinbin.initial_cooldown_seconds == 15

    def test_load_config_deserializes_query_orchestration_dict(self):
        """load_config must convert query_orchestration dict to QueryOrchestrationConfig."""
        import json
        import os
        import tempfile
        from code_indexer.server.utils.config_manager import (
            ServerConfigManager,
            QueryOrchestrationConfig,
        )

        config_data = {
            "server_dir": "/tmp/test",
            "query_orchestration": {
                "parallel_query_orchestrator_timeout_seconds": 30,
                "max_query_latency_budget_seconds": 120,
                "all_providers_sinbinned_retry_limit": 3,
                "provider_health_probe_interval_seconds": 15,
                "provider_health_probe_join_timeout_seconds": 10,
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = os.path.join(tmpdir, "config.json")
            with open(config_file, "w") as f:
                json.dump(config_data, f)
            manager = ServerConfigManager(server_dir_path=tmpdir)
            cfg = manager.load_config()
        assert isinstance(cfg.query_orchestration, QueryOrchestrationConfig)
        assert cfg.query_orchestration.parallel_query_orchestrator_timeout_seconds == 30
        assert cfg.query_orchestration.max_query_latency_budget_seconds == 120
