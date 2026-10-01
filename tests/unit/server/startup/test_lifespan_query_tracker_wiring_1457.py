"""Story #1457 AC1/AC2 live wiring, final piece: POST-HOC injection of the
server-wide QueryTracker singleton onto SemanticQueryManager.

Extracted as a small, dedicated, unit-testable helper
(_wire_query_tracker_into_semantic_query_manager) rather than left inline
in lifespan.py's large async startup generator -- which cannot be unit
tested in isolation without spinning up a full FastAPI app -- mirroring
the same reasoning that motivated extracting other startup wiring helpers
in this module (`_ensure_*` naming precedent).
"""

from __future__ import annotations

from unittest.mock import MagicMock


def test_wires_query_tracker_when_semantic_query_manager_present():
    from code_indexer.server.startup.lifespan import (
        _wire_query_tracker_into_semantic_query_manager,
    )

    fake_sqm = MagicMock()
    fake_app = MagicMock()
    fake_app.state.semantic_query_manager = fake_sqm
    fake_query_tracker = MagicMock()

    _wire_query_tracker_into_semantic_query_manager(fake_app, fake_query_tracker)

    fake_sqm.set_query_tracker.assert_called_once_with(fake_query_tracker)


def test_no_crash_when_semantic_query_manager_not_yet_wired():
    from code_indexer.server.startup.lifespan import (
        _wire_query_tracker_into_semantic_query_manager,
    )

    class _FakeState:
        pass

    class _FakeApp:
        state = _FakeState()

    _wire_query_tracker_into_semantic_query_manager(_FakeApp(), MagicMock())
