"""
Unit tests for Story #224: C7 removal from dependency_map_service.py.

C7: Remove _reindex_cidx_meta() method from DependencyMapService.
    RefreshScheduler handles indexing now. DependencyMapService must not
    call cidx index anymore.

Tests:
- test_reindex_removed_from_depmap: _reindex_cidx_meta method does not exist
"""

from unittest.mock import Mock

from code_indexer.server.services.dependency_map_service import DependencyMapService


class TestReindexRemovedFromDependencyMapService:
    """C7: _reindex_cidx_meta must be removed from DependencyMapService."""

    def test_reindex_removed_from_depmap(self):
        """
        _reindex_cidx_meta() must no longer exist on DependencyMapService.

        C7: RefreshScheduler handles cidx-meta indexing via versioned platform.
        DependencyMapService must not call cidx index directly.
        """
        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )

        assert not hasattr(service, "_reindex_cidx_meta"), (
            "_reindex_cidx_meta() must be removed from DependencyMapService "
            "(C7: RefreshScheduler owns cidx-meta indexing now)"
        )
