"""_wire_query_tracker_into_semantic_query_manager() also wires the SAME
QueryTracker singleton into ActivatedRepoManager (Story #1458 AC13) so the
deactivation drain has a real, live tracker to observe in production --
without this, ActivatedRepoManager.set_query_tracker() is never called and
the deactivation drain silently no-ops forever (fail-open, by design, but
never actually protecting anything).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from code_indexer.server.startup.lifespan import (
    _wire_query_tracker_into_semantic_query_manager,
)


class TestWireQueryTrackerIntoActivatedRepoManager:
    def test_wires_activated_repo_manager_when_present(self):
        mock_query_tracker = MagicMock()
        mock_semantic_query_manager = MagicMock()
        mock_activated_repo_manager = MagicMock()
        app = SimpleNamespace(
            state=SimpleNamespace(
                semantic_query_manager=mock_semantic_query_manager,
                activated_repo_manager=mock_activated_repo_manager,
            )
        )

        _wire_query_tracker_into_semantic_query_manager(app, mock_query_tracker)

        mock_activated_repo_manager.set_query_tracker.assert_called_once_with(
            mock_query_tracker
        )
        # Pre-existing behavior preserved.
        mock_semantic_query_manager.set_query_tracker.assert_called_once_with(
            mock_query_tracker
        )

    def test_safe_noop_when_both_collaborators_absent(self):
        mock_query_tracker = MagicMock()
        app = SimpleNamespace(
            state=SimpleNamespace(
                semantic_query_manager=None,
                activated_repo_manager=None,
            )
        )

        # Must not raise even when neither collaborator is present.
        _wire_query_tracker_into_semantic_query_manager(app, mock_query_tracker)
