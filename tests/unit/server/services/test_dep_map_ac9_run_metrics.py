"""
Unit tests for AC9: Quality metrics tracked across runs.

Story #216 AC9:
- DependencyMapTrackingBackend.record_run_metrics() stores metrics to SQLite
- DependencyMapTrackingBackend.get_run_history() retrieves recent runs
- DependencyMapDashboardService.get_job_status() includes run_history
"""

from unittest.mock import Mock


def _make_backend(tmp_path):
    from code_indexer.server.storage.sqlite_backends import DependencyMapTrackingBackend
    return DependencyMapTrackingBackend(str(tmp_path / "test.db"))


def _sample_metrics(**overrides):
    defaults = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "domain_count": 5,
        "total_chars": 12000,
        "edge_count": 8,
        "zero_char_domains": 1,
        "repos_analyzed": 10,
        "repos_skipped": 2,
        "pass1_duration_s": 30.5,
        "pass2_duration_s": 120.3,
    }
    defaults.update(overrides)
    return defaults


class TestRecordRunMetrics:
    """AC9: record_run_metrics() persists metrics to SQLite."""

    def test_method_exists_on_backend(self, tmp_path):
        """AC9: DependencyMapTrackingBackend has record_run_metrics method."""
        backend = _make_backend(tmp_path)
        assert hasattr(backend, "record_run_metrics"), (
            "DependencyMapTrackingBackend must have record_run_metrics method"
        )

    def test_stores_metrics_retrievable_via_get_run_history(self, tmp_path):
        """AC9: Stored metrics are retrievable via get_run_history."""
        backend = _make_backend(tmp_path)
        backend.record_run_metrics(_sample_metrics())

        history = backend.get_run_history(limit=5)
        assert len(history) == 1
        assert history[0]["domain_count"] == 5

    def test_all_fields_stored_correctly(self, tmp_path):
        """AC9: All metric fields are stored and retrievable with correct values."""
        backend = _make_backend(tmp_path)
        metrics = _sample_metrics(
            timestamp="2026-02-15T12:00:00+00:00",
            domain_count=7,
            total_chars=50000,
            edge_count=15,
            zero_char_domains=2,
            repos_analyzed=20,
            repos_skipped=3,
            pass1_duration_s=45.2,
            pass2_duration_s=210.8,
        )
        backend.record_run_metrics(metrics)

        row = backend.get_run_history(limit=1)[0]
        assert row["timestamp"] == "2026-02-15T12:00:00+00:00"
        assert row["domain_count"] == 7
        assert row["total_chars"] == 50000
        assert row["edge_count"] == 15
        assert row["zero_char_domains"] == 2
        assert row["repos_analyzed"] == 20
        assert row["repos_skipped"] == 3
        assert abs(row["pass1_duration_s"] - 45.2) < 0.01
        assert abs(row["pass2_duration_s"] - 210.8) < 0.01


class TestGetRunHistory:
    """AC9: get_run_history retrieves records in correct order."""

    def test_method_exists_on_backend(self, tmp_path):
        """AC9: DependencyMapTrackingBackend has get_run_history method."""
        backend = _make_backend(tmp_path)
        assert hasattr(backend, "get_run_history"), (
            "DependencyMapTrackingBackend must have get_run_history method"
        )

    def test_returns_empty_when_no_runs(self, tmp_path):
        """AC9: Returns empty list when no metrics recorded."""
        backend = _make_backend(tmp_path)
        assert backend.get_run_history() == []

    def test_returns_most_recent_first(self, tmp_path):
        """AC9: Most recently inserted runs appear first."""
        backend = _make_backend(tmp_path)
        for i in range(3):
            backend.record_run_metrics(_sample_metrics(
                timestamp=f"2026-01-0{i+1}T00:00:00+00:00",
                domain_count=i,
            ))

        history = backend.get_run_history(limit=5)
        assert len(history) == 3
        assert history[0]["domain_count"] == 2
        assert history[2]["domain_count"] == 0

    def test_respects_limit_parameter(self, tmp_path):
        """AC9: get_run_history returns at most `limit` records."""
        backend = _make_backend(tmp_path)
        for i in range(5):
            backend.record_run_metrics(_sample_metrics(domain_count=i))

        assert len(backend.get_run_history(limit=3)) == 3

    def test_default_limit_is_five(self, tmp_path):
        """AC9: Default limit is 5 records."""
        backend = _make_backend(tmp_path)
        for i in range(7):
            backend.record_run_metrics(_sample_metrics(domain_count=i))

        assert len(backend.get_run_history()) == 5


class TestDashboardRunHistory:
    """AC9: DependencyMapDashboardService.get_job_status includes run_history."""

    def _make_dashboard_service(self, tmp_path, run_count=2):
        from code_indexer.server.services.dependency_map_dashboard_service import (
            DependencyMapDashboardService,
        )
        from code_indexer.server.storage.sqlite_backends import DependencyMapTrackingBackend
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        backend = DependencyMapTrackingBackend(str(tmp_path / "test.db"))
        for i in range(run_count):
            backend.record_run_metrics(_sample_metrics(
                timestamp=f"2026-01-0{i+1}T00:00:00+00:00",
                domain_count=3 + i,
            ))

        config = ClaudeIntegrationConfig(
            dependency_map_enabled=True,
            dependency_map_interval_hours=24,
        )
        config_mgr = Mock()
        config_mgr.get_claude_integration_config.return_value = config

        dep_map_svc = Mock()
        dep_map_svc.detect_changes.return_value = ([], [], [])

        return DependencyMapDashboardService(backend, config_mgr, dep_map_svc)

    def test_get_job_status_includes_run_history_key(self, tmp_path):
        """AC9: get_job_status() result includes 'run_history' key."""
        svc = self._make_dashboard_service(tmp_path, run_count=2)
        result = svc.get_job_status()
        assert "run_history" in result, "get_job_status must include 'run_history' key"

    def test_run_history_has_correct_count(self, tmp_path):
        """AC9: run_history in job_status contains recorded runs."""
        svc = self._make_dashboard_service(tmp_path, run_count=2)
        result = svc.get_job_status()
        assert len(result["run_history"]) == 2

    def test_run_history_most_recent_first(self, tmp_path):
        """AC9: run_history is ordered most recent first."""
        svc = self._make_dashboard_service(tmp_path, run_count=3)
        result = svc.get_job_status()
        history = result["run_history"]
        assert history[0]["domain_count"] > history[-1]["domain_count"]
