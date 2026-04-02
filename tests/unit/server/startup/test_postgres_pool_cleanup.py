"""Tests for PostgreSQL connection pool atexit cleanup (Bug #567 fix).

Covers register_postgres_pool_atexit_cleanup() and _cleanup_postgres_pools():
- All pools accumulated (not overwritten like the old singleton)
- Cleanup closes every registered pool
- ValueError raised for invalid inputs
- Thread safety: concurrent registrations do not lose pools
"""

import threading
from unittest.mock import MagicMock


def _reset_module_state():
    """Reset module-level list between tests to avoid cross-test leakage."""
    import code_indexer.server.startup.service_init as m

    m._postgres_pools_for_cleanup.clear()


# ---------------------------------------------------------------------------
# register_postgres_pool_atexit_cleanup — input validation
# ---------------------------------------------------------------------------


def test_register_raises_for_none():
    """Passing None must raise ValueError immediately."""
    import pytest
    from code_indexer.server.startup.service_init import (
        register_postgres_pool_atexit_cleanup,
    )

    _reset_module_state()
    with pytest.raises(ValueError):
        register_postgres_pool_atexit_cleanup(None)


def test_register_raises_for_object_without_close():
    """Passing an object with no close() must raise ValueError."""
    import pytest
    from code_indexer.server.startup.service_init import (
        register_postgres_pool_atexit_cleanup,
    )

    _reset_module_state()
    with pytest.raises(ValueError):
        register_postgres_pool_atexit_cleanup(object())


# ---------------------------------------------------------------------------
# register_postgres_pool_atexit_cleanup — accumulation behaviour
# ---------------------------------------------------------------------------


def test_register_accumulates_multiple_pools():
    """Each call appends to the list; no pool is overwritten."""
    import code_indexer.server.startup.service_init as m
    from code_indexer.server.startup.service_init import (
        register_postgres_pool_atexit_cleanup,
    )

    _reset_module_state()
    pools = [MagicMock(spec=["close"]) for _ in range(3)]
    for p in pools:
        register_postgres_pool_atexit_cleanup(p)

    assert len(m._postgres_pools_for_cleanup) == 3
    # Identity-based check to avoid MagicMock equality/hash issues
    assert {id(x) for x in m._postgres_pools_for_cleanup} == {id(x) for x in pools}


# ---------------------------------------------------------------------------
# _cleanup_postgres_pools — closes all pools
# ---------------------------------------------------------------------------


def test_cleanup_closes_all_registered_pools():
    """_cleanup_postgres_pools() must call close() on every registered pool."""
    from code_indexer.server.startup.service_init import (
        _cleanup_postgres_pools,
        register_postgres_pool_atexit_cleanup,
    )

    _reset_module_state()
    pools = [MagicMock(spec=["close"]) for _ in range(5)]
    for p in pools:
        register_postgres_pool_atexit_cleanup(p)

    _cleanup_postgres_pools()

    for p in pools:
        p.close.assert_called_once()


def test_cleanup_clears_list_after_running():
    """After cleanup, the list must be empty (idempotent second run is a no-op)."""
    import code_indexer.server.startup.service_init as m
    from code_indexer.server.startup.service_init import (
        _cleanup_postgres_pools,
        register_postgres_pool_atexit_cleanup,
    )

    _reset_module_state()
    pool = MagicMock(spec=["close"])
    register_postgres_pool_atexit_cleanup(pool)
    _cleanup_postgres_pools()

    assert m._postgres_pools_for_cleanup == []

    # Second call is safe and does not raise
    _cleanup_postgres_pools()


def test_cleanup_continues_after_pool_close_error():
    """A failing pool.close() must not prevent other pools from being closed."""
    from code_indexer.server.startup.service_init import (
        _cleanup_postgres_pools,
        register_postgres_pool_atexit_cleanup,
    )

    _reset_module_state()
    bad_pool = MagicMock(spec=["close"])
    bad_pool.close.side_effect = RuntimeError("PG gone")
    good_pool = MagicMock(spec=["close"])

    register_postgres_pool_atexit_cleanup(bad_pool)
    register_postgres_pool_atexit_cleanup(good_pool)

    # Must not raise even though bad_pool.close() throws
    _cleanup_postgres_pools()

    good_pool.close.assert_called_once()


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_registrations_accumulate_all_pools():
    """Concurrent register calls must not drop any pool."""
    import code_indexer.server.startup.service_init as m
    from code_indexer.server.startup.service_init import (
        register_postgres_pool_atexit_cleanup,
    )

    _reset_module_state()
    N = 50
    pools = [MagicMock(spec=["close"]) for _ in range(N)]
    errors: list = []

    def register(p):
        try:
            register_postgres_pool_atexit_cleanup(p)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=register, args=(p,)) for p in pools]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive(), "Thread did not finish in time"

    assert errors == [], f"Unexpected errors: {errors}"
    assert len(m._postgres_pools_for_cleanup) == N
