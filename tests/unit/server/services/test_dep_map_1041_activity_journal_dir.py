"""
Bug #1041: Activity journal not visible cross-node in cluster.

Tests for DependencyMapService.get_activity_journal_dir() which returns
the shared NFS journal path under {golden_repos_dir}/.scratch/depmap-activity-journal/.
"""

from unittest.mock import MagicMock

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_service(golden_repos_dir: str) -> DependencyMapService:
    """Build a DependencyMapService with minimal mocked dependencies."""
    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = golden_repos_dir
    config_manager = MagicMock()
    tracking_backend = MagicMock()
    tracking_backend.get_tracking.return_value = {}
    analyzer = MagicMock()
    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=analyzer,
    )


class TestGetActivityJournalDir:
    """get_activity_journal_dir() returns shared NFS path for cross-node journal access."""

    def test_get_activity_journal_dir_returns_scratch_path(self, tmp_path):
        """Returns path rooted at golden_repos_dir/.scratch/depmap-activity-journal."""
        svc = _make_service(str(tmp_path))

        result = svc.get_activity_journal_dir()

        assert result is not None
        assert result == tmp_path / ".scratch" / "depmap-activity-journal"

    def test_get_activity_journal_dir_includes_correct_subdir(self, tmp_path):
        """Path contains .scratch and depmap-activity-journal as path components."""
        svc = _make_service(str(tmp_path))

        result = svc.get_activity_journal_dir()

        assert result is not None
        parts = result.parts
        assert ".scratch" in parts
        assert "depmap-activity-journal" in parts

    def test_get_activity_journal_dir_returns_none_when_manager_missing_attr(self):
        """Returns None when golden_repos_manager lacks golden_repos_dir attribute."""
        golden_repos_manager = MagicMock(spec=[])  # no attributes at all
        config_manager = MagicMock()
        tracking_backend = MagicMock()
        tracking_backend.get_tracking.return_value = {}
        analyzer = MagicMock()
        svc = DependencyMapService(
            golden_repos_manager=golden_repos_manager,
            config_manager=config_manager,
            tracking_backend=tracking_backend,
            analyzer=analyzer,
        )

        result = svc.get_activity_journal_dir()

        assert result is None
