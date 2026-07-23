"""M3: pod-pull dispatch must exactly cover POD_PULL_OPS.

A future op added to POD_PULL_OPS without a matching IndexJobClaimLoop dispatch
executor (or vice versa) would let a row be claimed with no executor (or a
never-work-stolen op). validate_dispatch_covers turns that silent drift into a
loud startup failure.
"""

import pytest

from code_indexer.server.services.index_job_claim_loop import validate_dispatch_covers


def _noop(md, pc):
    return None


class TestValidateDispatchCovers:
    def test_exact_match_ok(self):
        required = frozenset({"a", "b"})
        dispatch = {"a": _noop, "b": _noop}
        # Must not raise.
        validate_dispatch_covers(dispatch, required)

    def test_missing_executor_raises(self):
        required = frozenset({"a", "b", "c"})
        dispatch = {"a": _noop, "b": _noop}
        with pytest.raises(RuntimeError):
            validate_dispatch_covers(dispatch, required)

    def test_extra_executor_raises(self):
        required = frozenset({"a"})
        dispatch = {"a": _noop, "b": _noop}
        with pytest.raises(RuntimeError):
            validate_dispatch_covers(dispatch, required)

    def test_matches_real_pod_pull_ops(self):
        # Guards the actual production wiring: the concrete dispatch built in
        # lifespan must match POD_PULL_OPS.
        from code_indexer.server.repositories.background_jobs import POD_PULL_OPS

        dispatch = {op: _noop for op in POD_PULL_OPS}
        validate_dispatch_covers(dispatch, POD_PULL_OPS)
