"""
Tests for concurrency guard on generate_missing_descriptions (Finding 8).
"""


class TestGenerateDescriptionsConcurrency:
    """Finding 8: Only one generate_missing_descriptions can run at a time."""

    def test_lock_prevents_concurrent_execution(self):
        """When lock is held, a second acquire attempt fails."""
        from code_indexer.server.routers.diagnostics import _generate_descriptions_lock

        # Acquire the lock to simulate concurrent execution
        assert _generate_descriptions_lock.acquire(blocking=False)
        try:
            # Verify lock is held - second acquire must fail
            assert not _generate_descriptions_lock.acquire(blocking=False)
        finally:
            _generate_descriptions_lock.release()

    def test_lock_released_after_normal_execution(self):
        """Lock is available when no endpoint is running."""
        from code_indexer.server.routers.diagnostics import _generate_descriptions_lock

        # Lock should be available (no endpoint currently running)
        acquired = _generate_descriptions_lock.acquire(blocking=False)
        if acquired:
            _generate_descriptions_lock.release()
        assert acquired, "Lock should be available when no endpoint is running"
