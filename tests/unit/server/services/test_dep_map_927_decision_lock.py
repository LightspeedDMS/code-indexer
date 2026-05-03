"""Story #927 Phase 2: _scheduler_decision_lock context manager tests.

Tests cluster-aware non-blocking decision lock:
- Solo mode (threading.Lock, no pg_pool)
- Cluster mode (PG advisory lock via pg_pool mock)
- _is_cluster_mode (gate that routes decision lock to correct backend)
- _stable_int_hash determinism and PG bigint range
"""

import queue
import threading
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService

_BARRIER_TIMEOUT_SECONDS = 5
_THREAD_JOIN_TIMEOUT_SECONDS = 10


def _make_service(pg_pool=None, job_tracker=None):
    """Create DependencyMapService with minimal mocked dependencies."""
    golden_repos_manager = MagicMock()
    config_manager = MagicMock()
    tracking_backend = MagicMock()
    tracking_backend.get_tracking.return_value = {}
    analyzer = MagicMock()

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=analyzer,
        job_tracker=job_tracker,
        pg_pool=pg_pool,
    )


def _make_pg_pool_mock(fetchone_result):
    """Build a mock pg_pool/conn/cursor returning the given fetchone result.

    Factored out to eliminate duplication across cluster-mode tests.
    """
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = fetchone_result

    mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    mock_conn.transaction.return_value.__enter__ = MagicMock(return_value=MagicMock())
    mock_conn.transaction.return_value.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value = mock_cursor

    return mock_pool, mock_conn


class TestStableIntHash:
    """_stable_int_hash must be deterministic and produce PG-valid bigint values."""

    def test_same_input_produces_same_output(self):
        """Same string input always produces the same integer."""
        h1 = DependencyMapService._stable_int_hash("dep_map_scheduler_delta")
        h2 = DependencyMapService._stable_int_hash("dep_map_scheduler_delta")
        assert h1 == h2

    def test_different_inputs_produce_different_outputs(self):
        """Different strings typically produce different integers."""
        h1 = DependencyMapService._stable_int_hash("dep_map_scheduler_delta")
        h2 = DependencyMapService._stable_int_hash("dep_map_scheduler_refinement")
        assert h1 != h2

    def test_fits_pg_signed_bigint_range(self):
        """Result must fit in PostgreSQL's signed bigint range [-2^63, 2^63-1]."""
        for key in ["delta", "refinement", "repair", "full", "dep_map_scheduler_x"]:
            val = DependencyMapService._stable_int_hash(key)
            assert -(2**63) <= val <= 2**63 - 1, (
                f"Hash for {key!r} = {val} is outside PG bigint range"
            )

    def test_returns_integer_type(self):
        """Return type must be int."""
        result = DependencyMapService._stable_int_hash("any_key")
        assert isinstance(result, int)


class TestIsClusterMode:
    """_is_cluster_mode returns True iff pg_pool was injected (gates decision lock backend)."""

    def test_returns_false_when_no_pg_pool(self):
        service = _make_service(pg_pool=None)
        assert service._is_cluster_mode() is False

    def test_returns_true_when_pg_pool_injected(self):
        mock_pool = MagicMock()
        service = _make_service(pg_pool=mock_pool)
        assert service._is_cluster_mode() is True


class TestSchedulerDecisionLockSoloMode:
    """Solo mode: threading.Lock per key, non-blocking."""

    def test_yields_true_when_lock_acquired(self):
        """First acquirer gets True."""
        service = _make_service()
        with service._scheduler_decision_lock("delta") as acquired:
            assert acquired is True

    def test_yields_false_when_lock_already_held(self):
        """Second concurrent acquirer for the same key gets False."""
        service = _make_service()
        results: queue.Queue = queue.Queue()

        barrier = threading.Barrier(2)

        def first_thread():
            with service._scheduler_decision_lock("delta") as acquired:
                results.put(("first", acquired))
                barrier.wait(
                    timeout=_BARRIER_TIMEOUT_SECONDS
                )  # phase 1: second can try
                barrier.wait(
                    timeout=_BARRIER_TIMEOUT_SECONDS
                )  # phase 2: second finished

        def second_thread():
            barrier.wait(
                timeout=_BARRIER_TIMEOUT_SECONDS
            )  # wait until first holds lock
            with service._scheduler_decision_lock("delta") as acquired:
                results.put(("second", acquired))
            barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)  # signal first we're done

        t1 = threading.Thread(target=first_thread, daemon=True)
        t2 = threading.Thread(target=second_thread, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
        t2.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)

        assert not t1.is_alive(), "first_thread did not finish within timeout"
        assert not t2.is_alive(), "second_thread did not finish within timeout"

        collected = {}
        while not results.empty():
            name, val = results.get_nowait()
            collected[name] = val

        assert collected.get("first") is True
        assert collected.get("second") is False

    def test_lock_released_after_context_exits(self):
        """After the with block, the same key can be acquired again."""
        service = _make_service()

        with service._scheduler_decision_lock("delta") as first:
            assert first is True

        with service._scheduler_decision_lock("delta") as second:
            assert second is True

    def test_same_key_returns_same_lock_instance(self):
        """Calling with the same key twice reuses the exact same Lock object (identity)."""
        service = _make_service()

        with service._scheduler_decision_lock("delta"):
            pass
        lock_first = service._solo_decision_locks["delta"]

        with service._scheduler_decision_lock("delta"):
            pass
        lock_second = service._solo_decision_locks["delta"]

        assert lock_first is lock_second

    def test_different_keys_use_different_lock_instances(self):
        """Different keys each get their own distinct Lock instance."""
        service = _make_service()
        with service._scheduler_decision_lock("delta"):
            pass
        with service._scheduler_decision_lock("refinement"):
            pass

        lock_delta = service._solo_decision_locks["delta"]
        lock_refinement = service._solo_decision_locks["refinement"]
        assert lock_delta is not lock_refinement

    def test_lock_released_even_when_exception_raised_inside_block(self):
        """Lock is released when an exception propagates out of the with block."""
        service = _make_service()

        with pytest.raises(RuntimeError, match="deliberate error"):
            with service._scheduler_decision_lock("delta") as acquired:
                assert acquired is True
                raise RuntimeError("deliberate error")

        with service._scheduler_decision_lock("delta") as second:
            assert second is True


class TestSchedulerDecisionLockClusterMode:
    """Cluster mode: delegates to PG advisory lock via pg_pool."""

    def test_pg_try_advisory_xact_lock_called_when_acquired(self):
        """When PG returns True for advisory lock, yields True."""
        mock_pool, mock_conn = _make_pg_pool_mock(fetchone_result=(True,))
        service = _make_service(pg_pool=mock_pool)

        with service._scheduler_decision_lock("delta") as acquired:
            assert acquired is True

        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args
        assert "pg_try_advisory_xact_lock" in call_args[0][0]

    def test_yields_false_when_pg_lock_not_acquired(self):
        """When PG returns False for advisory lock, yields False."""
        mock_pool, mock_conn = _make_pg_pool_mock(fetchone_result=(False,))
        service = _make_service(pg_pool=mock_pool)

        with service._scheduler_decision_lock("delta") as acquired:
            assert acquired is False
