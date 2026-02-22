"""
Tests for TOCTOU race fix in DependencyMapService (Bug #256).

Demonstrates the TOCTOU race in the old pattern (is_available + thread.start)
and verifies the new atomic try_start_analysis() method prevents concurrent
analysis from being launched by two simultaneous callers.
"""

import threading
from unittest.mock import Mock


class TestTOCTOURaceOldPattern:
    """
    Demonstrates the TOCTOU race that exists when callers use
    is_available() then separately start a thread.

    These tests document the bug: two concurrent callers can BOTH pass
    the is_available() check because the lock is released between check
    and use.
    """

    def test_toctou_race_both_callers_pass_availability_check(self):
        """
        Prove the TOCTOU race: two threads calling is_available() concurrently
        can both get True before either has started the analysis thread.

        This simulates what happens when two concurrent HTTP requests arrive
        simultaneously at handle_trigger_dependency_analysis or
        trigger_dependency_map.
        """
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )

        results = []
        barrier = threading.Barrier(2)

        def caller():
            barrier.wait()
            result = service.is_available()
            results.append(result)

        t1 = threading.Thread(target=caller)
        t2 = threading.Thread(target=caller)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Both callers got True - THIS IS THE BUG.
        # In the real handler, both would then spawn analysis threads.
        assert results == [True, True], (
            "TOCTOU race confirmed: both concurrent callers got True from is_available(). "
            "This means both would proceed to start analysis threads."
        )


class TestTryStartAnalysis:
    """
    Tests for the new atomic try_start_analysis() method that fixes the
    TOCTOU race by atomically checking and marking busy in one operation.
    """

    def test_try_start_analysis_method_exists(self):
        """try_start_analysis() method must exist on DependencyMapService."""
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )
        assert hasattr(service, "try_start_analysis"), (
            "DependencyMapService must have try_start_analysis() method"
        )

    def test_try_start_analysis_returns_true_when_available(self):
        """try_start_analysis() returns True when no analysis is running."""
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )

        result = service.try_start_analysis("full")
        assert result is True

        service.release_analysis_guard()

    def test_try_start_analysis_no_race_two_concurrent_callers(self):
        """
        Core regression test: only ONE of two concurrent callers gets True.

        This is the atomic guarantee that fixes the TOCTOU race.
        Both callers hit the method simultaneously via a barrier; exactly
        one must succeed and the other must be rejected.
        """
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )

        results = []
        barrier = threading.Barrier(2)

        def caller():
            barrier.wait()
            result = service.try_start_analysis("full")
            results.append(result)

        t1 = threading.Thread(target=caller)
        t2 = threading.Thread(target=caller)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert sorted(results) == [False, True], (
            f"Expected exactly one True and one False, got: {results}. "
            "The TOCTOU race fix must guarantee only one caller proceeds."
        )

        service.release_analysis_guard()
