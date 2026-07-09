"""
Regression tests for JSONB dict vs JSON string handling in commit_hashes.

Bug: json.loads() called on already-parsed dict from PostgreSQL JSONB column.
- psycopg3 returns Python dict directly for JSONB columns (not a JSON string)
- Two locations call json.loads() without checking the type first
- File 1: DependencyMapService.detect_changes() — crashes with TypeError
- File 2: DependencyMapDashboardService._get_stored_hashes() — returns {} silently

Both locations must accept dict (PostgreSQL path) and str (SQLite path).
"""

import json
from unittest.mock import MagicMock


from .conftest import make_service


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_tracking_backend_with_commit_hashes(commit_hashes_value):
    """Return a mock tracking backend whose get_tracking() returns commit_hashes_value."""
    backend = MagicMock()
    backend.get_tracking.return_value = {"commit_hashes": commit_hashes_value}
    backend.update_tracking.return_value = None
    return backend


def _make_service_with_empty_repos(
    tracking_backend,
    mock_golden_repos_manager,
    mock_config_manager,
    mock_analyzer,
):
    """
    Create a DependencyMapService where _get_activated_repos and _enrich_repo_sizes
    return empty lists so detect_changes() reaches the hashes-parsing logic cleanly.
    """
    service = make_service(
        mock_golden_repos_manager,
        mock_config_manager,
        tracking_backend,
        mock_analyzer,
    )
    service._get_activated_repos = MagicMock(return_value=[])
    service._enrich_repo_sizes = MagicMock(return_value=[])
    return service


# ─────────────────────────────────────────────────────────────────────────────
# Tests: DependencyMapService.detect_changes() — commit_hashes type handling
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectChangesCommitHashesAsDict:
    """
    detect_changes() must not crash when tracking backend returns commit_hashes
    as a Python dict (PostgreSQL JSONB path via psycopg3).
    """

    def test_detect_changes_handles_commit_hashes_as_dict_without_crash(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_analyzer,
    ):
        """
        detect_changes() must not raise TypeError when commit_hashes is already a dict.

        Given tracking backend returns commit_hashes as a dict (PostgreSQL JSONB path)
        When detect_changes() is called
        Then it must not crash with TypeError("the JSON object must be str, bytes or bytearray, not dict")
        """
        stored_hashes = {"repo-alpha": "abc123", "repo-beta": "def456"}
        backend = _make_tracking_backend_with_commit_hashes(stored_hashes)
        service = _make_service_with_empty_repos(
            backend, mock_golden_repos_manager, mock_config_manager, mock_analyzer
        )

        # Must not raise TypeError
        changed, new, removed = service.detect_changes()

        assert changed == []
        assert new == []
        assert removed == ["repo-alpha", "repo-beta"]

    def test_detect_changes_uses_dict_hashes_for_change_detection(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_analyzer,
    ):
        """
        detect_changes() must correctly classify repos as removed when commit_hashes
        is a dict and none of the stored aliases appear in current repos.

        Given commit_hashes is already a dict with two aliases
        And current repos list is empty
        When detect_changes() is called
        Then both aliases must appear in removed_repos
        """
        stored = {"repo-x": "sha-111", "repo-y": "sha-222"}
        backend = _make_tracking_backend_with_commit_hashes(stored)
        service = _make_service_with_empty_repos(
            backend, mock_golden_repos_manager, mock_config_manager, mock_analyzer
        )

        _, _, removed = service.detect_changes()

        assert set(removed) == {"repo-x", "repo-y"}


class TestDetectChangesCommitHashesAsString:
    """
    detect_changes() must still work when tracking backend returns commit_hashes
    as a JSON string (SQLite path — existing behavior must not regress).
    """

    def test_detect_changes_handles_commit_hashes_as_json_string(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_analyzer,
    ):
        """
        detect_changes() must parse commit_hashes correctly when it is a JSON string.

        Given tracking backend returns commit_hashes as a JSON-encoded string (SQLite path)
        When detect_changes() is called
        Then it must not crash and stored hashes must be used correctly
        """
        stored = {"repo-alpha": "abc123"}
        backend = _make_tracking_backend_with_commit_hashes(json.dumps(stored))
        service = _make_service_with_empty_repos(
            backend, mock_golden_repos_manager, mock_config_manager, mock_analyzer
        )

        # Must not raise
        changed, new, removed = service.detect_changes()

        assert removed == ["repo-alpha"]

    def test_detect_changes_handles_none_commit_hashes(
        self,
        mock_golden_repos_manager,
        mock_config_manager,
        mock_analyzer,
    ):
        """
        detect_changes() must treat None commit_hashes as empty (first-run case).

        Given tracking backend returns commit_hashes as None
        When detect_changes() is called
        Then removed_repos is empty (nothing stored to compare against)
        """
        backend = _make_tracking_backend_with_commit_hashes(None)
        service = _make_service_with_empty_repos(
            backend, mock_golden_repos_manager, mock_config_manager, mock_analyzer
        )

        changed, new, removed = service.detect_changes()

        assert changed == []
        assert new == []
        assert removed == []


# ─────────────────────────────────────────────────────────────────────────────
# Tests: DependencyMapDashboardService._get_stored_hashes() — type handling
# ─────────────────────────────────────────────────────────────────────────────


def _make_dashboard_service(commit_hashes_value):
    """
    Create a DependencyMapDashboardService with a mock tracking backend
    returning the given commit_hashes_value.
    """
    from code_indexer.server.services.dependency_map_dashboard_service import (
        DependencyMapDashboardService,
    )
    from unittest.mock import Mock

    tracking_backend = Mock()
    tracking_backend.get_tracking.return_value = {
        "commit_hashes": commit_hashes_value,
        "status": "completed",
    }

    config = Mock()
    config.dependency_map_enabled = True
    config.dependency_map_interval_hours = 24
    config_manager = Mock()
    config_manager.get_claude_integration_config.return_value = config

    dep_map_service = Mock()
    dep_map_service.detect_changes.return_value = ([], [], [])

    return DependencyMapDashboardService(
        tracking_backend=tracking_backend,
        config_manager=config_manager,
        dependency_map_service=dep_map_service,
    )


class TestDashboardGetStoredHashesAsDict:
    """
    DependencyMapDashboardService._get_stored_hashes() must return the actual
    dict content when commit_hashes is already a dict (PostgreSQL JSONB path).

    The current code catches TypeError and returns {} silently — this hides
    the bug and causes the dashboard to show wrong information.
    """

    def test_get_stored_hashes_returns_actual_dict_when_value_is_dict(self):
        """
        _get_stored_hashes() must return the dict as-is when tracking returns a dict.

        Given commit_hashes is already a Python dict (PostgreSQL path)
        When _get_stored_hashes() is called
        Then the exact dict content must be returned (not an empty dict)
        """
        stored = {"repo-alpha": "abc123", "repo-beta": "def456"}
        service = _make_dashboard_service(stored)

        result = service._get_stored_hashes()

        assert result == stored

    def test_get_stored_hashes_returns_non_empty_dict_for_pg_path(self):
        """
        _get_stored_hashes() must not silently return {} when value is already a dict.

        This confirms the silent-failure bug is fixed: previously TypeError was caught
        and {} was returned instead of the real data.
        """
        stored = {"my-repo": "sha-cafebabe"}
        service = _make_dashboard_service(stored)

        result = service._get_stored_hashes()

        assert len(result) == 1
        assert result["my-repo"] == "sha-cafebabe"


class TestDashboardGetStoredHashesAsString:
    """
    DependencyMapDashboardService._get_stored_hashes() must still correctly parse
    commit_hashes when it is a JSON string (SQLite path — existing behavior).
    """

    def test_get_stored_hashes_parses_json_string_correctly(self):
        """
        _get_stored_hashes() must parse JSON string and return the dict.

        Given commit_hashes is a JSON-encoded string (SQLite path)
        When _get_stored_hashes() is called
        Then the parsed dict must be returned
        """
        stored = {"repo-sqlite": "sha-99aabb"}
        service = _make_dashboard_service(json.dumps(stored))

        result = service._get_stored_hashes()

        assert result == stored

    def test_get_stored_hashes_returns_empty_dict_for_none(self):
        """
        _get_stored_hashes() must return {} when commit_hashes is None.

        Given commit_hashes is None (no prior analysis)
        When _get_stored_hashes() is called
        Then an empty dict must be returned
        """
        service = _make_dashboard_service(None)

        result = service._get_stored_hashes()

        assert result == {}
