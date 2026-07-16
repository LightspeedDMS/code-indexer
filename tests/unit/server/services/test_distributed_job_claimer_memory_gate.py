"""memory-aware admission gate in DistributedJobClaimer.

A pod under memory pressure must decline to claim so the row stays 'pending'
for a pod with headroom (or for later). Uses a stub governor installed via
set_memory_governor — no real cgroup/psutil I/O.
"""

from unittest.mock import MagicMock

import pytest

from code_indexer.server.services import memory_governor as mg
from code_indexer.server.services.distributed_job_claimer import DistributedJobClaimer


class _StubGovernor:
    def __init__(self, allowed: bool):
        self.allowed = allowed

    def admission_allowed(self, max_used_pct: float) -> bool:
        return self.allowed


def _claimer(**kwargs):
    pool = MagicMock()
    claimer = DistributedJobClaimer(pool=pool, node_id="node-1", **kwargs)
    return claimer, pool


def _wire_empty_claim(pool):
    """Make pool.connection() yield a cursor whose claim SELECT finds nothing."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)


class TestClaimerMemoryGate:
    def teardown_method(self):
        mg.clear_memory_governor()

    def test_declines_claim_under_pressure(self):
        """Governor denies → return None and never touch the DB (row stays pending)."""
        claimer, pool = _claimer()
        mg.set_memory_governor(_StubGovernor(allowed=False))
        assert claimer.claim_next_job() is None
        assert pool.connection.call_count == 0

    def test_proceeds_when_governor_admits(self):
        """Governor admits → reach the claim SQL (no pending → None, but DB touched)."""
        claimer, pool = _claimer()
        _wire_empty_claim(pool)
        mg.set_memory_governor(_StubGovernor(allowed=True))
        assert claimer.claim_next_job() is None
        assert pool.connection.call_count == 1

    def test_no_governor_fails_open(self):
        """No governor singleton (CLI/solo) → proceed to claim."""
        claimer, pool = _claimer()
        _wire_empty_claim(pool)
        mg.clear_memory_governor()
        assert claimer.claim_next_job() is None
        assert pool.connection.call_count == 1

    def test_gate_disabled_skips_governor(self):
        """memory_gate_enabled=False → ignore governor entirely."""
        claimer, pool = _claimer(memory_gate_enabled=False)
        _wire_empty_claim(pool)
        mg.set_memory_governor(_StubGovernor(allowed=False))
        assert claimer.claim_next_job() is None
        assert pool.connection.call_count == 1

    def test_gate_defaults(self):
        claimer, _ = _claimer()
        assert claimer._memory_gate_enabled is True
        assert claimer._memory_max_used_pct == pytest.approx(80.0)
