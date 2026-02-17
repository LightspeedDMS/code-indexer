"""
Unit tests for DependencyMapDashboardService (Story #212, AC2, AC3, AC4).

Tests the service that computes job status data for the Dependency Map dashboard page.
Uses real ClaudeIntegrationConfig types to catch attribute mismatches (anti-mock methodology
applied selectively - mocking only infrastructure boundaries like tracking backend).

TDD RED PHASE: These tests are written before the production code exists.
"""

import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def enabled_config():
    """Return real ClaudeIntegrationConfig with dependency map enabled."""
    return ClaudeIntegrationConfig(
        dependency_map_enabled=True,
        dependency_map_interval_hours=24,
    )


@pytest.fixture
def disabled_config():
    """Return real ClaudeIntegrationConfig with dependency map disabled."""
    return ClaudeIntegrationConfig(
        dependency_map_enabled=False,
        dependency_map_interval_hours=24,
    )


def _make_config_manager(config: ClaudeIntegrationConfig):
    """Build a mock config_manager that returns the given config."""
    cm = Mock()
    cm.get_claude_integration_config.return_value = config
    return cm


def _make_tracking_backend(
    status: str = "completed",
    last_run: str = None,
    next_run: str = None,
    error_message: str = None,
    commit_hashes: str = None,
):
    """Build a mock tracking backend with the given tracking state."""
    backend = Mock()
    backend.get_tracking.return_value = {
        "id": 1,
        "status": status,
        "last_run": last_run,
        "next_run": next_run,
        "error_message": error_message,
        "commit_hashes": commit_hashes,
    }
    return backend


def _make_dep_map_service(changed=False, new=False):
    """Build a mock DependencyMapService with configurable detect_changes output."""
    svc = Mock()
    changed_repos = [{"alias": "repo1"}] if changed else []
    new_repos = [{"alias": "new-repo"}] if new else []
    svc.detect_changes.return_value = (changed_repos, new_repos, [])
    return svc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_ago_iso(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Import the service under test (will fail in RED phase)
# ─────────────────────────────────────────────────────────────────────────────


def _import_service():
    from code_indexer.server.services.dependency_map_dashboard_service import (
        DependencyMapDashboardService,
    )
    return DependencyMapDashboardService


# ─────────────────────────────────────────────────────────────────────────────
# AC3: Health Badge Computation - all 5 states
# ─────────────────────────────────────────────────────────────────────────────


class TestHealthComputation:
    """
    AC3: 5-state health model computed by _compute_health().

    States:
      GRAY   - dependency_map_enabled=false
      BLUE   - status=running
      RED    - status=failed OR last_run > 2x interval (stale)
      YELLOW - completed + (repos changed OR approaching stale at 75%)
      GREEN  - completed, fresh, no changed repos
    """

    def test_health_disabled_returns_gray(self, disabled_config):
        """AC3: When dependency_map_enabled=False, health is Disabled/GRAY."""
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(status="completed"),
            config_manager=_make_config_manager(disabled_config),
            dependency_map_service=_make_dep_map_service(),
        )

        result = service.get_job_status()

        assert result["health"] == "Disabled"
        assert result["color"] == "GRAY"

    def test_health_running_returns_blue(self, enabled_config):
        """AC3: When status=running, health is Running/BLUE."""
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="running",
                last_run=_hours_ago_iso(1),
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(),
        )

        result = service.get_job_status()

        assert result["health"] == "Running"
        assert result["color"] == "BLUE"

    def test_health_failed_returns_red(self, enabled_config):
        """AC3: When status=failed, health is Unhealthy/RED."""
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="failed",
                last_run=_hours_ago_iso(2),
                error_message="Analysis crashed",
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(),
        )

        result = service.get_job_status()

        assert result["health"] == "Unhealthy"
        assert result["color"] == "RED"

    def test_health_stale_last_run_returns_red(self, enabled_config):
        """AC3: When last_run > 2x interval hours ago, health is Unhealthy/RED."""
        # interval=24h, so stale threshold = 48h; last run was 50h ago
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="completed",
                last_run=_hours_ago_iso(50),  # 50h > 48h threshold
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(changed=False, new=False),
        )

        result = service.get_job_status()

        assert result["health"] == "Unhealthy"
        assert result["color"] == "RED"

    def test_health_completed_fresh_no_changes_returns_green(self, enabled_config):
        """AC3: Completed, fresh, no changed repos -> Healthy/GREEN."""
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="completed",
                last_run=_hours_ago_iso(1),  # very fresh, well within 48h threshold
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(changed=False, new=False),
        )

        result = service.get_job_status()

        assert result["health"] == "Healthy"
        assert result["color"] == "GREEN"

    def test_health_completed_with_changed_repos_returns_yellow(self, enabled_config):
        """AC3: Completed but repos changed since last run -> Degraded/YELLOW."""
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="completed",
                last_run=_hours_ago_iso(1),
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(changed=True, new=False),
        )

        result = service.get_job_status()

        assert result["health"] == "Degraded"
        assert result["color"] == "YELLOW"

    def test_health_completed_approaching_stale_returns_yellow(self, enabled_config):
        """AC3: Completed, 75% of 2x interval elapsed -> Degraded/YELLOW."""
        # interval=24h, stale threshold=48h, 75% = 36h
        # last_run was 37h ago -> approaching stale
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="completed",
                last_run=_hours_ago_iso(37),  # 37h > 36h (75% of 48h)
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(changed=False, new=False),
        )

        result = service.get_job_status()

        assert result["health"] == "Degraded"
        assert result["color"] == "YELLOW"

    def test_health_completed_with_new_repos_returns_yellow(self, enabled_config):
        """AC3: Completed but new repos detected -> Degraded/YELLOW."""
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="completed",
                last_run=_hours_ago_iso(1),
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(changed=False, new=True),
        )

        result = service.get_job_status()

        assert result["health"] == "Degraded"
        assert result["color"] == "YELLOW"

    def test_health_pending_no_last_run_returns_unhealthy(self, enabled_config):
        """AC3: Status=pending with no last_run -> Unhealthy/RED (fallback case)."""
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="pending",
                last_run=None,
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(),
        )

        result = service.get_job_status()

        assert result["health"] == "Unhealthy"
        assert result["color"] == "RED"


# ─────────────────────────────────────────────────────────────────────────────
# AC2: Job Status section data
# ─────────────────────────────────────────────────────────────────────────────


class TestGetJobStatus:
    """AC2: get_job_status() returns all required fields."""

    def test_get_job_status_returns_required_keys(self, enabled_config):
        """get_job_status() must return health, color, status, last_run, next_run, error_message."""
        DependencyMapDashboardService = _import_service()
        last_run = _hours_ago_iso(1)
        next_run = _now_iso()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="completed",
                last_run=last_run,
                next_run=next_run,
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(),
        )

        result = service.get_job_status()

        assert "health" in result
        assert "color" in result
        assert "status" in result
        assert "last_run" in result
        assert "next_run" in result
        assert "error_message" in result

    def test_get_job_status_passes_through_status(self, enabled_config):
        """get_job_status() passes raw status from tracking backend."""
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(status="running", last_run=_hours_ago_iso(0.5)),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(),
        )

        result = service.get_job_status()

        assert result["status"] == "running"

    def test_get_job_status_passes_through_timestamps(self, enabled_config):
        """get_job_status() passes last_run and next_run from tracking backend."""
        DependencyMapDashboardService = _import_service()
        last_run = _hours_ago_iso(2)
        next_run = _now_iso()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="completed",
                last_run=last_run,
                next_run=next_run,
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(),
        )

        result = service.get_job_status()

        assert result["last_run"] == last_run
        assert result["next_run"] == next_run

    def test_get_job_status_none_timestamps_when_no_runs(self, enabled_config):
        """get_job_status() returns None for timestamps when no runs have occurred."""
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="pending",
                last_run=None,
                next_run=None,
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(),
        )

        result = service.get_job_status()

        assert result["last_run"] is None
        assert result["next_run"] is None


# ─────────────────────────────────────────────────────────────────────────────
# AC4: Error banner - error_message passthrough
# ─────────────────────────────────────────────────────────────────────────────


class TestErrorMessage:
    """AC4: Error message is passed through to enable error banner rendering."""

    def test_error_message_present_when_failed(self, enabled_config):
        """AC4: error_message is non-empty when status=failed with error."""
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="failed",
                last_run=_hours_ago_iso(1),
                error_message="Claude CLI timed out after 1800 seconds",
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(),
        )

        result = service.get_job_status()

        assert result["error_message"] == "Claude CLI timed out after 1800 seconds"

    def test_error_message_none_when_completed(self, enabled_config):
        """AC4: error_message is None when status=completed (no banner)."""
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="completed",
                last_run=_hours_ago_iso(1),
                error_message=None,
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(),
        )

        result = service.get_job_status()

        assert result["error_message"] is None

    def test_error_message_preserved_for_xss_escaping_by_template(self, enabled_config):
        """AC4: error_message with HTML chars is passed through raw (template handles escaping)."""
        DependencyMapDashboardService = _import_service()
        raw_error = '<script>alert("xss")</script> Error occurred'
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="failed",
                last_run=_hours_ago_iso(1),
                error_message=raw_error,
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=_make_dep_map_service(),
        )

        result = service.get_job_status()

        # Service passes raw message; Jinja2 auto-escaping handles XSS prevention
        assert result["error_message"] == raw_error


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge cases for robustness."""

    def test_detect_changes_exception_treated_as_no_changes(self, enabled_config):
        """If detect_changes raises, service defaults to no-changes (safe degradation)."""
        DependencyMapDashboardService = _import_service()
        dep_svc = Mock()
        dep_svc.detect_changes.side_effect = RuntimeError("detect_changes failed")

        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="completed",
                last_run=_hours_ago_iso(1),
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=dep_svc,
        )

        # Should not raise; should still return a result
        result = service.get_job_status()

        assert "health" in result
        # With no changes detected (due to exception), should be GREEN (fresh, no changes)
        assert result["color"] == "GREEN"

    def test_no_dependency_map_service_still_works(self, enabled_config):
        """If dependency_map_service is None, health still computed without change detection."""
        DependencyMapDashboardService = _import_service()
        service = DependencyMapDashboardService(
            tracking_backend=_make_tracking_backend(
                status="completed",
                last_run=_hours_ago_iso(1),
            ),
            config_manager=_make_config_manager(enabled_config),
            dependency_map_service=None,
        )

        result = service.get_job_status()

        assert "health" in result
        # No service means no change detection, so treated as no changes -> GREEN
        assert result["color"] == "GREEN"


# ─────────────────────────────────────────────────────────────────────────────
# Story #213: Temp directory registry for cleanup
# ─────────────────────────────────────────────────────────────────────────────

_tmp_dirs_to_clean: list = []


@pytest.fixture(autouse=True, scope="session")
def _cleanup_tmp_dirs():
    """Clean up temp directories created by _make_service_for_coverage."""
    yield
    import shutil
    for d in _tmp_dirs_to_clean:
        shutil.rmtree(d, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# Story #213 Helper
# ─────────────────────────────────────────────────────────────────────────────


def _make_service_for_coverage(
    golden_repos=None,
    stored_hashes=None,
    domains_data=None,
    last_run_override="__DEFAULT__",
):
    """
    Build a DependencyMapDashboardService configured for coverage testing.

    Creates a mock dep_map_service with configurable golden repos and commit
    hashes. Sets service._current_commits_provider so tests can control
    per-repo commits via a callable(alias) -> commit_hash_or_None.

    Uses a registered temp directory for _domains.json when domains_data
    is provided; registers dir for session-scoped cleanup.

    Args:
        golden_repos: list of {"alias": str, "clone_path": str} dicts
        stored_hashes: dict mapping alias -> commit_hash (tracking data)
        domains_data: list of domain dicts for _domains.json (None = missing)
        last_run_override: ISO timestamp string for last_run in tracking, or None.
                           Pass "__DEFAULT__" (the default) to use _hours_ago_iso(1).
    """
    from code_indexer.server.services.dependency_map_dashboard_service import (
        DependencyMapDashboardService,
    )

    golden_repos = golden_repos or []
    stored_hashes = stored_hashes or {}

    # Resolve sentinel: if caller did not pass last_run_override, use default 1h ago
    if last_run_override == "__DEFAULT__":
        last_run = _hours_ago_iso(1)
    else:
        last_run = last_run_override

    dep_map_svc = Mock()
    activated = [
        {"alias": r["alias"], "clone_path": r.get("clone_path", f"/fake/{r['alias']}")}
        for r in golden_repos
    ]
    # Configure both private and public accessors on the mock so tests work
    # regardless of which the production code calls after the refactor.
    dep_map_svc._get_activated_repos.return_value = activated
    dep_map_svc.get_activated_repos.return_value = activated
    dep_map_svc._golden_repos_manager = Mock()
    dep_map_svc._golden_repos_manager.list_golden_repos.return_value = golden_repos
    dep_map_svc._golden_repos_manager.golden_repos_dir = "/fake/golden-repos"
    dep_map_svc.golden_repos_dir = "/fake/golden-repos"

    tracking_backend = Mock()
    tracking_backend.get_tracking.return_value = {
        "status": "completed",
        "last_run": last_run,
        "next_run": None,
        "error_message": None,
        "commit_hashes": json.dumps(stored_hashes) if stored_hashes else None,
    }

    config_manager = Mock()
    config_manager.get_claude_integration_config.return_value = Mock(
        dependency_map_enabled=True,
        dependency_map_interval_hours=24,
    )

    service = DependencyMapDashboardService(
        tracking_backend=tracking_backend,
        config_manager=config_manager,
        dependency_map_service=dep_map_svc,
    )

    # Inject domains file override (registered temp file or None)
    if domains_data is not None:
        tmpdir = tempfile.mkdtemp()
        _tmp_dirs_to_clean.append(tmpdir)
        domains_file = Path(tmpdir) / "_domains.json"
        domains_file.write_text(json.dumps(domains_data))
        service._domains_file_override = str(domains_file)
    else:
        service._domains_file_override = None

    # Default commits provider (returns None unless test overrides)
    service._current_commits_provider = lambda alias: None

    return service


# ─────────────────────────────────────────────────────────────────────────────
# Story #213 AC1 + AC2: Status Computation (NEW/OK/CHANGED/REMOVED) + Sort
# ─────────────────────────────────────────────────────────────────────────────


class TestRepoCoverageStatusComputation:
    """
    Story #213 AC2: Color-coded status badges for repo coverage.

    Status algorithm:
      NEW     (BLUE)   - alias not in stored tracking hashes
      OK      (GREEN)  - current commit == stored hash
      CHANGED (YELLOW) - current commit != stored hash
      REMOVED (GRAY)   - in stored hashes but not in current golden repos
    """

    def test_repo_not_in_tracking_is_new(self):
        """AC2: Repo not in stored hashes gets NEW (BLUE) status."""
        service = _make_service_for_coverage(
            golden_repos=[{"alias": "repo-a", "clone_path": "/fake/repo-a"}],
            stored_hashes={},
        )
        service._current_commits_provider = lambda alias: "abc123"

        result = service.get_repo_coverage()

        repos = result["repos"]
        assert len(repos) == 1
        assert repos[0]["alias"] == "repo-a"
        assert repos[0]["status"] == "NEW"
        assert repos[0]["status_color"] == "BLUE"

    def test_repo_with_matching_hash_is_ok(self):
        """AC2: Repo with same commit hash as stored gets OK (GREEN) status."""
        service = _make_service_for_coverage(
            golden_repos=[{"alias": "repo-b", "clone_path": "/fake/repo-b"}],
            stored_hashes={"repo-b": "deadbeef"},
        )
        service._current_commits_provider = lambda alias: "deadbeef"

        result = service.get_repo_coverage()

        repos = result["repos"]
        assert len(repos) == 1
        assert repos[0]["alias"] == "repo-b"
        assert repos[0]["status"] == "OK"
        assert repos[0]["status_color"] == "GREEN"

    def test_repo_with_different_hash_is_changed(self):
        """AC2: Repo with different commit than stored gets CHANGED (YELLOW) status."""
        service = _make_service_for_coverage(
            golden_repos=[{"alias": "repo-c", "clone_path": "/fake/repo-c"}],
            stored_hashes={"repo-c": "oldcommit"},
        )
        service._current_commits_provider = lambda alias: "newcommit"

        result = service.get_repo_coverage()

        repos = result["repos"]
        assert len(repos) == 1
        assert repos[0]["alias"] == "repo-c"
        assert repos[0]["status"] == "CHANGED"
        assert repos[0]["status_color"] == "YELLOW"

    def test_stored_alias_not_in_current_repos_is_removed(self):
        """AC2: Alias in stored hashes but not in golden repos gets REMOVED (GRAY) status."""
        service = _make_service_for_coverage(
            golden_repos=[],
            stored_hashes={"old-repo": "abc123"},
        )
        service._current_commits_provider = lambda alias: None

        result = service.get_repo_coverage()

        repos = result["repos"]
        assert len(repos) == 1
        assert repos[0]["alias"] == "old-repo"
        assert repos[0]["status"] == "REMOVED"
        assert repos[0]["status_color"] == "GRAY"

    def test_repos_sorted_alphabetically(self):
        """AC1: Repos in table are sorted alphabetically by alias."""
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "zebra-repo", "clone_path": "/fake/zebra"},
                {"alias": "alpha-repo", "clone_path": "/fake/alpha"},
                {"alias": "middle-repo", "clone_path": "/fake/middle"},
            ],
            stored_hashes={},
        )
        service._current_commits_provider = lambda alias: "abc"

        result = service.get_repo_coverage()

        aliases = [r["alias"] for r in result["repos"]]
        assert aliases == sorted(aliases)


# ─────────────────────────────────────────────────────────────────────────────
# Story #213 AC3: Coverage Percentage Calculation
# ─────────────────────────────────────────────────────────────────────────────


class TestRepoCoverageCalculation:
    """
    Story #213 AC3: Coverage percentage calculation.

    Coverage = (OK + CHANGED) / (total active repos) * 100
    REMOVED excluded from both numerator and denominator.
    Color thresholds: green >80%, yellow 50-80%, red <50%.
    """

    def test_coverage_pct_all_ok(self):
        """AC3: All repos OK -> 100% coverage."""
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "repo-1", "clone_path": "/fake/1"},
                {"alias": "repo-2", "clone_path": "/fake/2"},
            ],
            stored_hashes={"repo-1": "hash1", "repo-2": "hash2"},
        )
        commits = {"repo-1": "hash1", "repo-2": "hash2"}
        service._current_commits_provider = lambda alias: commits[alias]

        result = service.get_repo_coverage()

        assert result["covered_count"] == 2
        assert result["total_count"] == 2
        assert result["coverage_pct"] == 100.0

    def test_coverage_pct_with_changed_counts_as_covered(self):
        """AC3: CHANGED repos count as covered in the percentage."""
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "repo-1", "clone_path": "/fake/1"},
                {"alias": "repo-2", "clone_path": "/fake/2"},
            ],
            stored_hashes={"repo-1": "hash1", "repo-2": "old-hash"},
        )
        commits = {"repo-1": "hash1", "repo-2": "new-hash"}
        service._current_commits_provider = lambda alias: commits[alias]

        result = service.get_repo_coverage()

        assert result["covered_count"] == 2
        assert result["coverage_pct"] == 100.0

    def test_coverage_pct_new_repos_not_covered(self):
        """AC3: NEW repos are not counted as covered."""
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "repo-1", "clone_path": "/fake/1"},
                {"alias": "repo-new", "clone_path": "/fake/new"},
            ],
            stored_hashes={"repo-1": "hash1"},
        )
        service._current_commits_provider = lambda alias: "some-hash"

        result = service.get_repo_coverage()

        assert result["covered_count"] == 1
        assert result["total_count"] == 2
        assert result["coverage_pct"] == 50.0

    def test_coverage_pct_removed_excluded_from_total(self):
        """AC3: REMOVED repos excluded from both numerator and denominator."""
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "repo-1", "clone_path": "/fake/1"},
            ],
            stored_hashes={"repo-1": "hash1", "removed-repo": "old-hash"},
        )
        service._current_commits_provider = lambda alias: "hash1"

        result = service.get_repo_coverage()

        assert result["covered_count"] == 1
        assert result["total_count"] == 1
        assert result["coverage_pct"] == 100.0

    def test_coverage_pct_zero_when_no_active_repos(self):
        """AC3: Zero active repos results in 0% (not division by zero)."""
        service = _make_service_for_coverage(
            golden_repos=[],
            stored_hashes={},
        )
        service._current_commits_provider = lambda alias: None

        result = service.get_repo_coverage()

        assert result["coverage_pct"] == 0.0
        assert result["total_count"] == 0
        assert result["covered_count"] == 0

    def test_coverage_color_green_above_80_pct(self):
        """AC3: Coverage >80% gives green progress bar color."""
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "r1", "clone_path": "/f/1"},
                {"alias": "r2", "clone_path": "/f/2"},
            ],
            stored_hashes={"r1": "h1", "r2": "h2"},
        )
        commits = {"r1": "h1", "r2": "h2"}
        service._current_commits_provider = lambda alias: commits[alias]

        result = service.get_repo_coverage()

        assert result["coverage_color"] == "green"

    def test_coverage_color_yellow_50_to_80_pct(self):
        """AC3: Coverage 50-80% gives yellow progress bar color."""
        # 3/5 = 60% -> yellow
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "r1", "clone_path": "/f/1"},
                {"alias": "r2", "clone_path": "/f/2"},
                {"alias": "r3", "clone_path": "/f/3"},
                {"alias": "r4", "clone_path": "/f/4"},
                {"alias": "r5", "clone_path": "/f/5"},
            ],
            stored_hashes={"r1": "h1", "r2": "h2", "r3": "h3"},
        )
        service._current_commits_provider = lambda alias: {
            "r1": "h1", "r2": "h2", "r3": "h3", "r4": "x", "r5": "x"
        }[alias]

        result = service.get_repo_coverage()

        assert result["coverage_pct"] == 60.0
        assert result["coverage_color"] == "yellow"

    def test_coverage_color_red_below_50_pct(self):
        """AC3: Coverage <50% gives red progress bar color."""
        # 1/3 = 33% -> red
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "r1", "clone_path": "/f/1"},
                {"alias": "r2", "clone_path": "/f/2"},
                {"alias": "r3", "clone_path": "/f/3"},
            ],
            stored_hashes={"r1": "h1"},
        )
        service._current_commits_provider = lambda alias: {"r1": "h1", "r2": "x", "r3": "x"}[alias]

        result = service.get_repo_coverage()

        assert result["coverage_color"] == "red"


# ─────────────────────────────────────────────────────────────────────────────
# Story #213 AC5: Domain Column from _domains.json
# ─────────────────────────────────────────────────────────────────────────────


class TestRepoDomainMapping:
    """
    Story #213 AC5: Domain column from _domains.json participating_repos.

    Reverse map: repo_alias -> list of domain names.
    Missing _domains.json handled gracefully with empty lists.
    """

    def test_domain_map_populated_from_domains_json(self):
        """AC5: Domains extracted from _domains.json and reverse-mapped to repos."""
        domains_data = [
            {"name": "auth-domain", "description": "Auth", "participating_repos": ["repo-a", "repo-b"]},
            {"name": "data-domain", "description": "Data", "participating_repos": ["repo-b", "repo-c"]},
        ]
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "repo-a", "clone_path": "/f/a"},
                {"alias": "repo-b", "clone_path": "/f/b"},
                {"alias": "repo-c", "clone_path": "/f/c"},
            ],
            stored_hashes={},
            domains_data=domains_data,
        )
        service._current_commits_provider = lambda alias: "abc"

        result = service.get_repo_coverage()

        repos_by_alias = {r["alias"]: r for r in result["repos"]}
        assert repos_by_alias["repo-a"]["domains"] == ["auth-domain"]
        assert set(repos_by_alias["repo-b"]["domains"]) == {"auth-domain", "data-domain"}
        assert repos_by_alias["repo-c"]["domains"] == ["data-domain"]

    def test_unanalyzed_repo_shows_empty_domain_list(self):
        """AC5: Repo not in any domain shows empty list."""
        domains_data = [
            {"name": "domain-x", "description": "X", "participating_repos": ["repo-a"]},
        ]
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "repo-a", "clone_path": "/f/a"},
                {"alias": "repo-z", "clone_path": "/f/z"},
            ],
            stored_hashes={},
            domains_data=domains_data,
        )
        service._current_commits_provider = lambda alias: "abc"

        result = service.get_repo_coverage()

        repos_by_alias = {r["alias"]: r for r in result["repos"]}
        assert repos_by_alias["repo-z"]["domains"] == []

    def test_missing_domains_json_handled_gracefully(self):
        """AC5: When _domains.json missing, repos show empty domain list (no crash)."""
        service = _make_service_for_coverage(
            golden_repos=[{"alias": "repo-a", "clone_path": "/f/a"}],
            stored_hashes={},
            domains_data=None,
        )
        service._current_commits_provider = lambda alias: "abc"

        result = service.get_repo_coverage()

        repos = result["repos"]
        assert len(repos) == 1
        assert repos[0]["domains"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Story #213 AC4: Access-Controlled Filtering
# ─────────────────────────────────────────────────────────────────────────────


class TestRepoCoverageAccessFiltering:
    """
    Story #213 AC4: Access-controlled filtering.

    Admin (accessible_repos=None): sees all repos.
    Non-admin (accessible_repos=set): filtered to accessible set.
    Progress bar reflects filtered counts only.
    """

    def test_admin_sees_all_repos(self):
        """AC4: Admin (accessible_repos=None) sees all repos."""
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "repo-1", "clone_path": "/f/1"},
                {"alias": "repo-2", "clone_path": "/f/2"},
                {"alias": "repo-3", "clone_path": "/f/3"},
            ],
            stored_hashes={},
        )
        service._current_commits_provider = lambda alias: "abc"

        result = service.get_repo_coverage(accessible_repos=None)

        assert len(result["repos"]) == 3

    def test_non_admin_sees_only_accessible_repos(self):
        """AC4: Non-admin with accessible_repos set sees only filtered repos."""
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "repo-1", "clone_path": "/f/1"},
                {"alias": "repo-2", "clone_path": "/f/2"},
                {"alias": "repo-3", "clone_path": "/f/3"},
            ],
            stored_hashes={},
        )
        service._current_commits_provider = lambda alias: "abc"

        result = service.get_repo_coverage(accessible_repos={"repo-1", "repo-3"})

        aliases = [r["alias"] for r in result["repos"]]
        assert "repo-1" in aliases
        assert "repo-3" in aliases
        assert "repo-2" not in aliases

    def test_progress_bar_reflects_filtered_count(self):
        """AC4: total_count reflects accessible repos only, not full set."""
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "repo-1", "clone_path": "/f/1"},
                {"alias": "repo-2", "clone_path": "/f/2"},
                {"alias": "repo-3", "clone_path": "/f/3"},
            ],
            stored_hashes={"repo-1": "h1", "repo-3": "h3"},
        )
        commits = {"repo-1": "h1", "repo-2": "abc", "repo-3": "h3"}
        service._current_commits_provider = lambda alias: commits[alias]

        result = service.get_repo_coverage(accessible_repos={"repo-1", "repo-3"})

        assert result["total_count"] == 2
        assert result["covered_count"] == 2
        assert result["coverage_pct"] == 100.0

    def test_removed_repos_excluded_after_access_filter(self):
        """AC4: REMOVED repos excluded from totals even in filtered view."""
        service = _make_service_for_coverage(
            golden_repos=[
                {"alias": "repo-1", "clone_path": "/f/1"},
            ],
            stored_hashes={"repo-1": "h1", "old-repo": "old"},
        )
        service._current_commits_provider = lambda alias: "h1" if alias == "repo-1" else None

        result = service.get_repo_coverage(accessible_repos={"repo-1"})

        aliases = [r["alias"] for r in result["repos"]]
        assert "old-repo" not in aliases
        assert result["total_count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Story #213 Code Review Finding: last_analyzed timestamp column (AC1)
# ─────────────────────────────────────────────────────────────────────────────


class TestLastAnalyzedTimestamp:
    """
    Story #213 code review finding: AC1 specifies four columns including
    'Last Analyzed timestamp'. Each repo dict must include a 'last_analyzed'
    field. NEW repos (not in stored hashes) show None; OK/CHANGED repos
    show the tracking backend's last_run timestamp.
    """

    def test_new_repo_has_none_last_analyzed(self):
        """NEW repos not in stored hashes have last_analyzed=None."""
        service = _make_service_for_coverage(
            golden_repos=[{"alias": "repo-a", "clone_path": "/fake/a"}],
            stored_hashes={},  # repo-a not in stored -> NEW
        )
        service._current_commits_provider = lambda alias: "abc123"

        result = service.get_repo_coverage()

        repos = result["repos"]
        assert len(repos) == 1
        assert repos[0]["status"] == "NEW"
        assert "last_analyzed" in repos[0]
        assert repos[0]["last_analyzed"] is None

    def test_ok_repo_has_last_run_timestamp(self):
        """OK repos (hash matches) have last_analyzed equal to tracking last_run."""
        last_run = _hours_ago_iso(3)
        service = _make_service_for_coverage(
            golden_repos=[{"alias": "repo-b", "clone_path": "/fake/b"}],
            stored_hashes={"repo-b": "deadbeef"},
            last_run_override=last_run,
        )
        service._current_commits_provider = lambda alias: "deadbeef"

        result = service.get_repo_coverage()

        repos = result["repos"]
        assert len(repos) == 1
        assert repos[0]["status"] == "OK"
        assert "last_analyzed" in repos[0]
        assert repos[0]["last_analyzed"] == last_run

    def test_changed_repo_has_last_run_timestamp(self):
        """CHANGED repos (hash differs) have last_analyzed equal to tracking last_run."""
        last_run = _hours_ago_iso(5)
        service = _make_service_for_coverage(
            golden_repos=[{"alias": "repo-c", "clone_path": "/fake/c"}],
            stored_hashes={"repo-c": "oldcommit"},
            last_run_override=last_run,
        )
        service._current_commits_provider = lambda alias: "newcommit"

        result = service.get_repo_coverage()

        repos = result["repos"]
        assert len(repos) == 1
        assert repos[0]["status"] == "CHANGED"
        assert "last_analyzed" in repos[0]
        assert repos[0]["last_analyzed"] == last_run

    def test_removed_repo_has_last_run_timestamp(self):
        """REMOVED repos (in stored but not current) also carry last_analyzed."""
        last_run = _hours_ago_iso(2)
        service = _make_service_for_coverage(
            golden_repos=[],
            stored_hashes={"old-repo": "abc123"},
            last_run_override=last_run,
        )
        service._current_commits_provider = lambda alias: None

        result = service.get_repo_coverage()

        repos = result["repos"]
        assert len(repos) == 1
        assert repos[0]["status"] == "REMOVED"
        assert "last_analyzed" in repos[0]
        assert repos[0]["last_analyzed"] == last_run

    def test_no_last_run_in_tracking_gives_none_for_all(self):
        """When tracking has no last_run, all repos show last_analyzed=None."""
        service = _make_service_for_coverage(
            golden_repos=[{"alias": "repo-x", "clone_path": "/fake/x"}],
            stored_hashes={"repo-x": "abc"},
            last_run_override=None,
        )
        service._current_commits_provider = lambda alias: "abc"

        result = service.get_repo_coverage()

        repos = result["repos"]
        assert len(repos) == 1
        assert repos[0]["last_analyzed"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Story #213 Code Review Finding 2: Public accessor methods on DependencyMapService
# ─────────────────────────────────────────────────────────────────────────────


class TestDependencyMapServicePublicAccessors:
    """
    Code review finding: dashboard service accesses private members of
    DependencyMapService. DependencyMapService must expose public accessors.
    """

    def test_get_activated_repos_public_method_exists(self):
        """DependencyMapService.get_activated_repos() must be a public method."""
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        assert hasattr(DependencyMapService, "get_activated_repos"), (
            "DependencyMapService must have a public get_activated_repos() method"
        )

    def test_golden_repos_dir_public_property_exists(self):
        """DependencyMapService.golden_repos_dir must be a public property."""
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        assert hasattr(DependencyMapService, "golden_repos_dir"), (
            "DependencyMapService must have a public golden_repos_dir property"
        )

    def test_get_activated_repos_delegates_to_private(self):
        """get_activated_repos() returns same result as _get_activated_repos()."""
        from unittest.mock import Mock, patch
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        golden_repos_manager = Mock()
        golden_repos_manager.list_golden_repos.return_value = []
        golden_repos_manager.golden_repos_dir = "/fake/dir"

        config_manager = Mock()
        tracking_backend = Mock()
        analyzer = Mock()

        svc = DependencyMapService(
            golden_repos_manager=golden_repos_manager,
            config_manager=config_manager,
            tracking_backend=tracking_backend,
            analyzer=analyzer,
        )

        # Both should return the same result
        private_result = svc._get_activated_repos()
        public_result = svc.get_activated_repos()
        assert public_result == private_result

    def test_golden_repos_dir_property_returns_manager_dir(self):
        """golden_repos_dir property returns the golden_repos_manager's directory."""
        from unittest.mock import Mock
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        golden_repos_manager = Mock()
        golden_repos_manager.list_golden_repos.return_value = []
        golden_repos_manager.golden_repos_dir = "/expected/golden/dir"

        config_manager = Mock()
        tracking_backend = Mock()
        analyzer = Mock()

        svc = DependencyMapService(
            golden_repos_manager=golden_repos_manager,
            config_manager=config_manager,
            tracking_backend=tracking_backend,
            analyzer=analyzer,
        )

        assert svc.golden_repos_dir == "/expected/golden/dir"
