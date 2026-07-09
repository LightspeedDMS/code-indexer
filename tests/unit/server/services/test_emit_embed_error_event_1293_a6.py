"""Story #1293 S1b [A6]: failover error event.

emit_embed_error_event(provider_name) is the shared helper used at a failed
LIVE embedding call site (failover primary attempt) to record a durable
outcome=error/role=direct/live_batch_id=None event BEFORE the caller fails
over to a secondary provider. It routes through decide_role_and_outcome(error=True)
(the single source of truth, M1) rather than hardcoding the error row.
"""

from unittest.mock import patch


def test_emit_embed_error_event_uses_decision_table_error_row():
    from code_indexer.server.services.search_embed_event_emit import (
        emit_embed_error_event,
    )

    with patch(
        "code_indexer.server.services.search_embed_event_emit.emit_embed_event"
    ) as mock_emit:
        emit_embed_error_event("voyage-ai")

    mock_emit.assert_called_once()
    meta = mock_emit.call_args[0][0]
    assert meta.provider == "voyage-ai"
    assert meta.outcome == "error"
    assert meta.role == "direct"
    assert meta.live_batch_id is None


def test_emit_embed_error_event_never_raises_on_emit_failure():
    """Messi #13: telemetry emission must never mask the real failure path."""
    from code_indexer.server.services.search_embed_event_emit import (
        emit_embed_error_event,
    )

    with patch(
        "code_indexer.server.services.search_embed_event_emit.emit_embed_event",
        side_effect=RuntimeError("writer boom"),
    ):
        emit_embed_error_event("cohere")  # must not raise
