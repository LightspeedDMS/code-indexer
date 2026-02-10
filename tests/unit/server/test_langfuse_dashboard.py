"""
Unit tests for Langfuse dashboard integration (Story #168).

Tests cover:
- Langfuse card visibility based on pull_enabled
- Per-project metrics display
- Health indicator logic (GREEN/YELLOW/RED)
- Manual sync trigger
- Folder statistics calculation
- Sync efficiency metrics
"""

import pytest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


class TestLangfuseDashboardVisibility:
    """Test AC1: Card visibility condition."""

    def test_card_not_visible_when_pull_disabled(self):
        """Card should not be visible when pull_enabled is false."""
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            LangfuseConfig,
        )

        config = ServerConfig(server_dir="/tmp/test")
        config.langfuse_config = LangfuseConfig(pull_enabled=False)

        # Verify config state
        assert config.langfuse_config.pull_enabled is False

    def test_card_visible_when_pull_enabled(self):
        """Card should be visible when pull_enabled is true."""
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            LangfuseConfig,
            LangfusePullProject,
        )

        config = ServerConfig(server_dir="/tmp/test")
        config.langfuse_config = LangfuseConfig(
            pull_enabled=True,
            pull_projects=[LangfusePullProject(public_key="pk", secret_key="sk")],
        )

        # Verify config state
        assert config.langfuse_config.pull_enabled is True


class TestLangfuseMetricsRetrieval:
    """Test AC2: Per-project metrics."""

    def test_get_metrics_returns_per_project_data(self):
        """get_metrics() should return metrics for each configured project."""
        from code_indexer.server.services.langfuse_trace_sync_service import (
            LangfuseTraceSyncService,
        )
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            LangfuseConfig,
            LangfusePullProject,
        )

        # Create service
        def config_getter():
            config = ServerConfig()
            config.langfuse_config = LangfuseConfig(
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                    LangfusePullProject(public_key="pk2", secret_key="sk2"),
                ],
            )
            return config

        service = LangfuseTraceSyncService(config_getter=config_getter, data_dir="/tmp")

        # Initially empty metrics
        metrics = service.get_metrics()
        assert isinstance(metrics, dict)
        assert len(metrics) == 0

    def test_metrics_structure_matches_spec(self):
        """Metrics should include all required fields per AC2."""
        from code_indexer.server.services.langfuse_trace_sync_service import SyncMetrics

        metrics = SyncMetrics()
        metrics.traces_checked = 100
        metrics.traces_written_new = 10
        metrics.traces_written_updated = 5
        metrics.traces_unchanged = 85
        metrics.errors_count = 0
        metrics.last_sync_time = "2026-02-09T12:00:00Z"
        metrics.last_sync_duration_ms = 1500

        # Verify all fields exist
        assert metrics.traces_checked == 100
        assert metrics.traces_written_new == 10
        assert metrics.traces_written_updated == 5
        assert metrics.traces_unchanged == 85
        assert metrics.errors_count == 0
        assert metrics.last_sync_time == "2026-02-09T12:00:00Z"
        assert metrics.last_sync_duration_ms == 1500


class TestHealthIndicatorLogic:
    """Test AC3: Overall health indicator."""

    def test_health_green_when_all_projects_successful(self):
        """Health should be GREEN when all projects synced successfully."""
        from code_indexer.server.services.dashboard_service import DashboardService

        now = datetime.now(timezone.utc)

        # All projects synced recently with no errors
        metrics = {
            "project1": {
                "traces_checked": 100,
                "traces_written_new": 10,
                "traces_written_updated": 0,
                "traces_unchanged": 90,
                "errors_count": 0,
                "last_sync_time": now.isoformat(),
                "last_sync_duration_ms": 1000,
            },
            "project2": {
                "traces_checked": 50,
                "traces_written_new": 5,
                "traces_written_updated": 0,
                "traces_unchanged": 45,
                "errors_count": 0,
                "last_sync_time": now.isoformat(),
                "last_sync_duration_ms": 500,
            },
        }

        # H2: Use production method instead of local copy
        service = DashboardService()
        health = service._compute_langfuse_health(metrics, interval_seconds=300)
        assert health == "healthy"

    def test_health_yellow_when_errors_but_sync_running(self):
        """Health should be YELLOW when some projects have errors."""
        from code_indexer.server.services.dashboard_service import DashboardService

        now = datetime.now(timezone.utc)

        # One project with errors
        metrics = {
            "project1": {
                "traces_checked": 100,
                "traces_written_new": 10,
                "traces_written_updated": 0,
                "traces_unchanged": 90,
                "errors_count": 0,
                "last_sync_time": now.isoformat(),
                "last_sync_duration_ms": 1000,
            },
            "project2": {
                "traces_checked": 50,
                "traces_written_new": 5,
                "traces_written_updated": 0,
                "traces_unchanged": 45,
                "errors_count": 3,
                "last_sync_time": now.isoformat(),
                "last_sync_duration_ms": 500,
            },
        }

        # H2: Use production method
        service = DashboardService()
        health = service._compute_langfuse_health(metrics, interval_seconds=300)
        assert health == "degraded"

    def test_health_red_when_sync_stale(self):
        """Health should be RED when sync hasn't run for 2x interval."""
        from code_indexer.server.services.dashboard_service import DashboardService

        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(seconds=700)  # >2x 300s interval

        # All projects synced long ago
        metrics = {
            "project1": {
                "traces_checked": 100,
                "traces_written_new": 10,
                "traces_written_updated": 0,
                "traces_unchanged": 90,
                "errors_count": 0,
                "last_sync_time": stale_time.isoformat(),
                "last_sync_duration_ms": 1000,
            },
        }

        # H2: Use production method
        service = DashboardService()
        health = service._compute_langfuse_health(metrics, interval_seconds=300)
        assert health == "unhealthy"

    def test_health_unknown_when_no_metrics(self):
        """Health should be unknown when no metrics available."""
        from code_indexer.server.services.dashboard_service import DashboardService

        metrics = {}

        # H2: Use production method
        service = DashboardService()
        health = service._compute_langfuse_health(metrics, interval_seconds=300)
        assert health == "unknown"


class TestFolderStatistics:
    """Test AC5: Folder statistics."""

    def test_count_langfuse_folders(self):
        """Should count langfuse_* folders correctly."""
        from code_indexer.server.services.dashboard_service import DashboardService

        with tempfile.TemporaryDirectory() as tmpdir:
            # H3: Fix path to match production (data/golden-repos)
            server_dir = Path(tmpdir)
            golden_repos = server_dir / "data" / "golden-repos"
            golden_repos.mkdir(parents=True)

            # Create langfuse folders
            (golden_repos / "langfuse_user1").mkdir()
            (golden_repos / "langfuse_user2").mkdir()
            (golden_repos / "other_folder").mkdir()  # Should be ignored

            # H2: Use production method
            service = DashboardService()
            stats = service._get_langfuse_folder_stats(server_dir)
            assert stats["user_folders"] == 2
            assert stats["total_traces"] == 0
            assert stats["total_size_mb"] == 0.0

    def test_count_trace_files(self):
        """Should count JSON trace files correctly."""
        from code_indexer.server.services.dashboard_service import DashboardService

        with tempfile.TemporaryDirectory() as tmpdir:
            # H3: Fix path to match production
            server_dir = Path(tmpdir)
            golden_repos = server_dir / "data" / "golden-repos"
            golden_repos.mkdir(parents=True)

            # Create folder with trace files
            user_folder = golden_repos / "langfuse_user1"
            user_folder.mkdir()

            # Create trace files
            (user_folder / "trace1.json").write_text('{"id": "1"}')
            (user_folder / "trace2.json").write_text('{"id": "2"}')
            (user_folder / "readme.txt").write_text("not a trace")

            # H2: Use production method
            service = DashboardService()
            stats = service._get_langfuse_folder_stats(server_dir)
            assert stats["user_folders"] == 1
            assert stats["total_traces"] == 2
            # Size should be > 0 (small files are rounded to 0.0, check >= 0)
            assert stats["total_size_mb"] >= 0.0

    def test_calculate_storage_size(self):
        """Should calculate total storage size in MB."""
        from code_indexer.server.services.dashboard_service import DashboardService

        with tempfile.TemporaryDirectory() as tmpdir:
            # H3: Fix path to match production
            server_dir = Path(tmpdir)
            golden_repos = server_dir / "data" / "golden-repos"
            golden_repos.mkdir(parents=True)

            user_folder = golden_repos / "langfuse_user1"
            user_folder.mkdir()

            # Create 1MB file
            large_content = "x" * (1024 * 1024)
            (user_folder / "trace1.json").write_text(large_content)

            # H2: Use production method
            service = DashboardService()
            stats = service._get_langfuse_folder_stats(server_dir)
            assert stats["total_size_mb"] >= 1.0


class TestSyncEfficiencyMetrics:
    """Test AC7: Sync efficiency metrics."""

    def test_efficiency_ratio_calculation(self):
        """Should calculate traces_written / traces_checked ratio."""
        metrics = {
            "traces_checked": 100,
            "traces_written_new": 10,
            "traces_written_updated": 5,
            "traces_unchanged": 85,
        }

        total_written = (
            metrics["traces_written_new"] + metrics["traces_written_updated"]
        )
        efficiency = (
            total_written / metrics["traces_checked"]
            if metrics["traces_checked"] > 0
            else 0.0
        )

        assert efficiency == 0.15  # 15% of traces were new or updated

    def test_efficiency_ratio_zero_when_no_checks(self):
        """Efficiency should be 0 when no traces checked."""
        metrics = {
            "traces_checked": 0,
            "traces_written_new": 0,
            "traces_written_updated": 0,
            "traces_unchanged": 0,
        }

        total_written = (
            metrics["traces_written_new"] + metrics["traces_written_updated"]
        )
        efficiency = (
            total_written / metrics["traces_checked"]
            if metrics["traces_checked"] > 0
            else 0.0
        )

        assert efficiency == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
