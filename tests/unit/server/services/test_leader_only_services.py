"""Bug #580: Verify leader-only gating of housekeeping services in lifespan.py.

These tests inspect the lifespan source code to verify that:
1. Leader election callbacks (_on_become_leader / _on_lose_leadership) are wired.
2. JobReconciliationService is NOT started unconditionally.
3. SelfMonitoringService is gated behind storage_mode != "postgres".
"""

import inspect
import re


def _get_lifespan_source() -> str:
    """Return the source code of the lifespan module."""
    from code_indexer.server.startup import lifespan as lifespan_mod

    return inspect.getsource(lifespan_mod)


class TestBug580LeaderOnlyServices:
    """Bug #580: Housekeeping services must be gated behind leader election."""

    def test_lifespan_sets_leader_callbacks(self):
        """Verify _on_become_leader and _on_lose_leadership are assigned
        to the leader election service in the lifespan code."""
        source = _get_lifespan_source()

        assert "_leader_election._on_become_leader = _on_become_leader" in source, (
            "Leader election _on_become_leader callback not wired in lifespan"
        )

        assert "_leader_election._on_lose_leadership = _on_lose_leadership" in source, (
            "Leader election _on_lose_leadership callback not wired in lifespan"
        )

    def test_reconciliation_not_started_unconditionally(self):
        """Verify _reconciliation.start() is NOT called directly outside
        of the leader callback. It should only appear inside _on_become_leader."""
        source = _get_lifespan_source()

        # Find all _reconciliation.start() calls
        pattern = re.compile(r"_reconciliation\.start\(\)")
        matches = list(pattern.finditer(source))

        assert len(matches) > 0, (
            "_reconciliation.start() not found at all -- "
            "it should exist inside _on_become_leader callback"
        )

        # Verify each call is inside _on_become_leader, not at top level
        for match in matches:
            # Get the 500 chars before the match to check context
            start = max(0, match.start() - 500)
            context_before = source[start : match.start()]
            assert "def _on_become_leader" in context_before, (
                "_reconciliation.start() found outside of _on_become_leader callback. "
                "Bug #580 requires it to only run via leader election."
            )

    def test_self_monitoring_gated_by_storage_mode(self):
        """Verify self_monitoring_service.start() is gated behind
        storage_mode != 'postgres' for the initial startup path."""
        source = _get_lifespan_source()

        # Find the standalone-mode guard for self_monitoring_service.start()
        assert 'storage_mode != "postgres"' in source, (
            "storage_mode != 'postgres' guard not found in lifespan -- "
            "Bug #580 requires self-monitoring to not auto-start in cluster mode"
        )

        # Verify self_monitoring_service.start() also exists in the leader callback
        leader_callback_match = re.search(
            r"def _on_become_leader\(\):(.*?)(?=\n                    def |\n                    _leader_election)",
            source,
            re.DOTALL,
        )
        assert leader_callback_match is not None, (
            "_on_become_leader function not found in lifespan source"
        )

        callback_body = leader_callback_match.group(1)
        assert "self_monitoring_service.start()" in callback_body, (
            "self_monitoring_service.start() not found inside _on_become_leader -- "
            "Bug #580 requires leader callback to start self-monitoring in cluster mode"
        )

    def test_leader_callback_stops_services_on_lose_leadership(self):
        """Verify _on_lose_leadership stops both services."""
        source = _get_lifespan_source()

        lose_match = re.search(
            r"def _on_lose_leadership\(\):(.*?)(?=\n                    _leader_election)",
            source,
            re.DOTALL,
        )
        assert lose_match is not None, (
            "_on_lose_leadership function not found in lifespan source"
        )

        callback_body = lose_match.group(1)
        assert "_reconciliation.stop()" in callback_body, (
            "_reconciliation.stop() not found in _on_lose_leadership callback"
        )
        assert "self_monitoring_service.stop()" in callback_body, (
            "self_monitoring_service.stop() not found in _on_lose_leadership callback"
        )

    def test_leader_callback_handles_stop_exceptions(self):
        """Verify _on_lose_leadership uses LOG+RECOVER for stop errors."""
        source = _get_lifespan_source()

        lose_match = re.search(
            r"def _on_lose_leadership\(\):(.*?)(?=\n                    _leader_election)",
            source,
            re.DOTALL,
        )
        assert lose_match is not None

        callback_body = lose_match.group(1)
        # Should have try/except with logging, not bare pass
        assert "except Exception as e:" in callback_body, (
            "_on_lose_leadership should catch exceptions with 'as e' for logging"
        )
        assert "logger.warning" in callback_body, (
            "_on_lose_leadership should log warnings on stop failures (LOG+RECOVER)"
        )
