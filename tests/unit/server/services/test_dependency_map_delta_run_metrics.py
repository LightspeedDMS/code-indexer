"""
Unit tests for Bug #572: Delta analysis runs not recorded in Recent Run Metrics table.

Tests verify that:
1. Delta analysis completion calls _record_run_metrics
2. Metrics recorded by delta have correct domain_count, total_chars, edge_count
3. Dashboard run_history includes delta run entries (integration with SQLite backend)
"""

from unittest.mock import Mock

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_service(tmp_path):
    """Build a minimal DependencyMapService for testing."""
    gm = Mock()
    gm.golden_repos_dir = str(tmp_path)
    tracking = Mock()
    tracking.get_tracking.return_value = {"status": "pending", "commit_hashes": None}
    config_mgr = Mock()
    analyzer = Mock()
    return DependencyMapService(gm, config_mgr, tracking, analyzer)


class TestDeltaAnalysisCallsRecordRunMetrics:
    """Bug #572: _finalize_delta_tracking path must also call _record_run_metrics."""

    def test_delta_completion_calls_record_run_metrics(self, tmp_path):
        """After _finalize_delta_tracking, _record_run_metrics must be called."""
        svc = _make_service(tmp_path)

        dep_map_dir = tmp_path / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        (dep_map_dir / "auth.md").write_text("authentication domain content")

        config = Mock()
        config.dependency_map_interval_hours = 24

        all_repos = [
            {"alias": "repo1", "clone_path": str(tmp_path / "repo1")},
        ]

        affected_domains = {"auth"}
        svc._finalize_delta_tracking(
            config, all_repos, output_dir=dep_map_dir, affected_domains=affected_domains
        )

        assert svc._tracking_backend.record_run_metrics.called, (
            "Bug #572: _record_run_metrics must be called after _finalize_delta_tracking"
        )


class TestDeltaRunMetricsValues:
    """Bug #572: Metrics recorded by delta have correct domain_count, total_chars, edge_count."""

    def test_delta_metrics_have_correct_domain_count(self, tmp_path):
        """Delta metrics domain_count reflects affected domains written to disk."""
        svc = _make_service(tmp_path)

        dep_map_dir = tmp_path / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        (dep_map_dir / "auth.md").write_text("auth content")
        (dep_map_dir / "payments.md").write_text("payments content")

        config = Mock()
        config.dependency_map_interval_hours = 24
        all_repos = [{"alias": "repo1", "clone_path": str(tmp_path / "repo1")}]

        affected_domains = {"auth", "payments"}
        svc._finalize_delta_tracking(
            config, all_repos, output_dir=dep_map_dir, affected_domains=affected_domains
        )

        call_args = svc._tracking_backend.record_run_metrics.call_args
        metrics = call_args[0][0]
        assert metrics["domain_count"] == 2

    def test_delta_metrics_have_correct_total_chars(self, tmp_path):
        """Delta metrics total_chars sums file sizes of affected domain files."""
        svc = _make_service(tmp_path)

        dep_map_dir = tmp_path / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        (dep_map_dir / "auth.md").write_text("AAAA")
        (dep_map_dir / "payments.md").write_text("BBBBBB")

        config = Mock()
        config.dependency_map_interval_hours = 24
        all_repos = [{"alias": "repo1", "clone_path": str(tmp_path / "repo1")}]

        affected_domains = {"auth", "payments"}
        svc._finalize_delta_tracking(
            config, all_repos, output_dir=dep_map_dir, affected_domains=affected_domains
        )

        call_args = svc._tracking_backend.record_run_metrics.call_args
        metrics = call_args[0][0]
        assert metrics["total_chars"] == 10

    def test_delta_metrics_have_correct_edge_count(self, tmp_path):
        """Delta metrics edge_count counts cross-domain dependency rows from _index.md."""
        svc = _make_service(tmp_path)

        dep_map_dir = tmp_path / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        (dep_map_dir / "auth.md").write_text("auth content")

        index_content = """# Dependency Map Index

## Cross-Domain Dependencies

| Source | Target | Type |
|--------|--------|------|
| auth | payments | import |
| auth | logging | call |

# End
"""
        (dep_map_dir / "_index.md").write_text(index_content)

        config = Mock()
        config.dependency_map_interval_hours = 24
        all_repos = [{"alias": "repo1", "clone_path": str(tmp_path / "repo1")}]

        affected_domains = {"auth"}
        svc._finalize_delta_tracking(
            config, all_repos, output_dir=dep_map_dir, affected_domains=affected_domains
        )

        call_args = svc._tracking_backend.record_run_metrics.call_args
        metrics = call_args[0][0]
        assert metrics["edge_count"] == 2

    def test_delta_metrics_pass_durations_are_zero(self, tmp_path):
        """Delta analysis has no pass1/pass2 split, so durations are 0.0."""
        svc = _make_service(tmp_path)

        dep_map_dir = tmp_path / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        (dep_map_dir / "auth.md").write_text("auth content")

        config = Mock()
        config.dependency_map_interval_hours = 24
        all_repos = [{"alias": "repo1", "clone_path": str(tmp_path / "repo1")}]

        affected_domains = {"auth"}
        svc._finalize_delta_tracking(
            config, all_repos, output_dir=dep_map_dir, affected_domains=affected_domains
        )

        call_args = svc._tracking_backend.record_run_metrics.call_args
        metrics = call_args[0][0]
        assert metrics["pass1_duration_s"] == 0.0
        assert metrics["pass2_duration_s"] == 0.0

    def test_delta_metrics_repos_analyzed_count(self, tmp_path):
        """Delta metrics repos_analyzed reflects all_repos length."""
        svc = _make_service(tmp_path)

        dep_map_dir = tmp_path / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        (dep_map_dir / "auth.md").write_text("auth content")

        config = Mock()
        config.dependency_map_interval_hours = 24
        all_repos = [
            {"alias": "repo1", "clone_path": str(tmp_path / "repo1")},
            {"alias": "repo2", "clone_path": str(tmp_path / "repo2")},
        ]

        affected_domains = {"auth"}
        svc._finalize_delta_tracking(
            config, all_repos, output_dir=dep_map_dir, affected_domains=affected_domains
        )

        call_args = svc._tracking_backend.record_run_metrics.call_args
        metrics = call_args[0][0]
        assert metrics["repos_analyzed"] == 2


class TestDeltaRunMetricsIntegration:
    """Bug #572: Integration test - dashboard run_history includes delta run entries."""

    def test_delta_metrics_stored_in_sqlite_backend(self, tmp_path):
        """Delta run metrics end up in the run_history table via real SQLite backend."""
        from code_indexer.server.storage.sqlite_backends import (
            DependencyMapTrackingBackend,
        )

        db_path = tmp_path / "test.db"
        backend = DependencyMapTrackingBackend(str(db_path))

        gm = Mock()
        gm.golden_repos_dir = str(tmp_path)
        config_mgr = Mock()
        analyzer = Mock()
        svc = DependencyMapService(gm, config_mgr, backend, analyzer)

        dep_map_dir = tmp_path / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        (dep_map_dir / "auth.md").write_text("auth domain content here")

        config = Mock()
        config.dependency_map_interval_hours = 24
        all_repos = [{"alias": "repo1", "clone_path": str(tmp_path / "repo1")}]

        # Initialize the tracking table (creates singleton row)
        backend.get_tracking()

        affected_domains = {"auth"}
        svc._finalize_delta_tracking(
            config, all_repos, output_dir=dep_map_dir, affected_domains=affected_domains
        )

        history = backend.get_run_history(limit=10)
        assert len(history) >= 1, "Delta run must appear in run_history"
        latest = history[0]
        assert latest["domain_count"] == 1
        assert latest["total_chars"] == len("auth domain content here")
