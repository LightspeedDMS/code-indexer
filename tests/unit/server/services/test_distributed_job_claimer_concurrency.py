"""
Tests for Bug #541: Cluster-wide job concurrency enforcement.

Verifies that DistributedJobClaimer.claim_next_job() checks the total
running job count across all nodes before claiming, when
max_concurrent_jobs > 0.
"""

from unittest.mock import MagicMock

from code_indexer.server.services.distributed_job_claimer import DistributedJobClaimer


def _make_claimer(max_concurrent_jobs=0):
    """Create a DistributedJobClaimer with mocked pool."""
    mock_pool = MagicMock()
    claimer = DistributedJobClaimer(
        pool=mock_pool, node_id="node-1", max_concurrent_jobs=max_concurrent_jobs
    )
    return claimer, mock_pool


class TestClusterWideConcurrency:
    """Bug #541: Cluster-wide job concurrency enforcement."""

    def test_skips_claim_when_at_limit(self):
        """Must return None when running count >= max_concurrent_jobs."""
        claimer, pool = _make_claimer(max_concurrent_jobs=3)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (3,)  # At limit
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        result = claimer.claim_next_job()
        assert result is None

    def test_skips_claim_when_over_limit(self):
        """Must return None when running count > max_concurrent_jobs."""
        claimer, pool = _make_claimer(max_concurrent_jobs=3)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (5,)  # Over limit
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        result = claimer.claim_next_job()
        assert result is None

    def test_claims_when_under_limit(self):
        """Must proceed to claim when running count < max_concurrent_jobs."""
        claimer, pool = _make_claimer(max_concurrent_jobs=3)

        # Two connection calls: first for count check, second for claim
        call_count = {"n": 0}

        def conn_enter():
            call_count["n"] += 1
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            if call_count["n"] == 1:
                # First call: running count check — under limit
                mock_cursor.fetchone.return_value = (2,)
            else:
                # Second call: claim — no pending jobs
                mock_cursor.fetchone.return_value = None
            mock_conn.cursor.return_value.__enter__ = MagicMock(
                return_value=mock_cursor
            )
            mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            return mock_conn

        pool.connection.return_value.__enter__ = MagicMock(side_effect=conn_enter)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        result = claimer.claim_next_job()
        # Result is None because no pending jobs, but importantly the
        # count check passed (didn't short-circuit) — we reached the
        # second connection call (the claim attempt).
        assert result is None
        assert call_count["n"] == 2  # Both count check AND claim were executed

    def test_no_limit_when_zero(self):
        """When max_concurrent_jobs=0, no cluster check is performed."""
        claimer, pool = _make_claimer(max_concurrent_jobs=0)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None  # No pending jobs
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        result = claimer.claim_next_job()
        assert result is None  # No pending jobs, but no concurrency block

        # Verify only ONE connection was made (claim, no count check)
        assert pool.connection.call_count == 1

    def test_constructor_stores_max_concurrent_jobs(self):
        """max_concurrent_jobs must be stored on the instance."""
        claimer, _ = _make_claimer(max_concurrent_jobs=5)
        assert claimer._max_concurrent_jobs == 5

    def test_default_max_concurrent_jobs_is_zero(self):
        """Default max_concurrent_jobs is 0 (disabled)."""
        pool = MagicMock()
        claimer = DistributedJobClaimer(pool=pool, node_id="node-1")
        assert claimer._max_concurrent_jobs == 0
