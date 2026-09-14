"""Bug #1822 Defect 1: the deep-fidelity audit dispatched off
FilesystemVectorStore.search() must use its OWN dedicated, small,
bounded-capacity ThreadPoolExecutor + non-blocking gate -- NEVER the
caller's shared ``parallel_executor`` (the same 256-worker pool that also
serves real request work; see search_service.py's _get_query_executor()).

Bug #1813 (commit fefe1705) correctly moved the audit off the synchronous
request path but dispatched it straight onto that shared executor. At
production scale (~900 repos), a multi-repo request can produce hundreds of
sampled cache hits, all queuing on the SAME executor real request work
(index-load/embedding fan-out) also depends on -- unbounded telemetry
starving real work.

See ``_deep_fidelity_audit_test_support_1822.py`` for shared fixtures/
helpers and the full patching-pattern rationale.

RED evidence (pre-fix code, recorded 2026-09-08): every test in this file
fails at first attribute access on ``fvs._deep_fidelity_audit_gate`` /
``fvs._deep_fidelity_audit_executor`` / ``fvs._DEEP_FIDELITY_AUDIT_GATE_CAPACITY``
with:
    AttributeError: module 'code_indexer.storage.filesystem_vector_store'
    has no attribute '_deep_fidelity_audit_gate'
-- direct proof the current code has no bounded-capacity primitive at all,
i.e. genuinely unbounded.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from code_indexer.storage import filesystem_vector_store as fvs

from _deep_fidelity_audit_test_support_1822 import (
    COLLECTION_NAME,
    EVENT_WAIT_TIMEOUT_SECS,
    EXECUTOR_WORKERS,
    GRACE_WINDOW_SECS,
    MAX_REASONABLE_GATE_CAPACITY,
    PROMPT_RETURN_CEILING_SECS,
    REEMBED_SLEEP_SECS,
    SEARCH_LIMIT,
    assert_audit_dispatched_to_dedicated_executor,
    assert_gate_at_full_capacity,
    drain_gate,
    make_event_signaling_reembed,
    make_fake_provider,
    make_counting_hnsw_query,
    patched_audit_sampled_search,
    restore_gate,
    run_sampled_search,
    submitted_fn_names,
)

# tiny_fsv_store is a pytest fixture provided by conftest.py in this same
# directory -- deliberately NOT imported here (see conftest.py's docstring
# on that fixture for why: importing a fixture function and using its name
# as a test-parameter reads to ruff as an unused import shadowed by that
# parameter).


class TestAuditGateCapacityConstant:
    """The capacity is a small, fixed internal constant -- never a runtime
    setting -- and the gate object faithfully enforces it."""

    def test_capacity_is_small_and_fixed(self):
        capacity = fvs._DEEP_FIDELITY_AUDIT_GATE_CAPACITY
        assert isinstance(capacity, int)
        assert 1 <= capacity <= MAX_REASONABLE_GATE_CAPACITY, (
            f"expected a small, fixed audit gate capacity (<= "
            f"{MAX_REASONABLE_GATE_CAPACITY}); got {capacity}"
        )

    def test_gate_is_bounded_semaphore_matching_capacity_constant(self):
        gate = fvs._deep_fidelity_audit_gate
        assert isinstance(gate, threading.Semaphore)

        drained = drain_gate(gate)
        try:
            assert drained == fvs._DEEP_FIDELITY_AUDIT_GATE_CAPACITY, (
                "gate's actual spare capacity does not match the declared "
                f"capacity constant: drained {drained}, constant is "
                f"{fvs._DEEP_FIDELITY_AUDIT_GATE_CAPACITY}"
            )
        finally:
            restore_gate(gate, drained)

        # A BoundedSemaphore raises ValueError on over-release; assert that
        # directly to prove it is bounded, not a plain Semaphore.
        gate.acquire(blocking=False)
        gate.release()
        with pytest.raises(ValueError):
            gate.release()


class TestAuditDispatchTarget:
    """Direct proof the audit worker is submitted to the dedicated audit
    executor and NEVER to the caller-supplied parallel_executor."""

    def test_search_dispatches_audit_to_dedicated_executor_not_parallel_executor(
        self, tiny_fsv_store
    ):
        store, vecs = tiny_fsv_store
        query_vec = vecs[0]
        fake_provider = make_fake_provider(query_vec)

        real_parallel = ThreadPoolExecutor(max_workers=EXECUTOR_WORKERS)
        spy_parallel = MagicMock(wraps=real_parallel)
        spy_audit = MagicMock(wraps=fvs._deep_fidelity_audit_executor)

        reembed_fn, _started, reembed_done = make_event_signaling_reembed(query_vec)
        hnsw_fn, hnsw_done, _calls = make_counting_hnsw_query()

        try:
            with patched_audit_sampled_search(
                query_vec,
                reembed_fn,
                hnsw_fn,
                audit_executor_override=spy_audit,
            ):
                results = store.search(
                    query="hello",
                    embedding_provider=fake_provider,
                    collection_name=COLLECTION_NAME,
                    limit=SEARCH_LIMIT,
                    parallel_executor=spy_parallel,
                )
                assert isinstance(results, list)
                assert reembed_done.wait(timeout=EVENT_WAIT_TIMEOUT_SECS), (
                    "audit's re-embed call never ran"
                )
                assert hnsw_done.wait(timeout=EVENT_WAIT_TIMEOUT_SECS), (
                    "audit's second HNSW search never ran"
                )
                audit_names = submitted_fn_names(spy_audit)
                parallel_names = submitted_fn_names(spy_parallel)
        finally:
            real_parallel.shutdown(wait=True)

        assert_audit_dispatched_to_dedicated_executor(audit_names, parallel_names)


class TestAuditCapacityBoundedBehavior:
    """The gate actually bounds outstanding audits, skips observably when
    exhausted, and always frees back up so later audits still run."""

    def test_capacity_exhausted_skips_audit_observably_and_search_still_returns(
        self, tiny_fsv_store, caplog
    ):
        store, vecs = tiny_fsv_store
        query_vec = vecs[0]
        fake_provider = make_fake_provider(query_vec)
        executor = ThreadPoolExecutor(max_workers=EXECUTOR_WORKERS)

        gate = fvs._deep_fidelity_audit_gate
        drained = drain_gate(gate)
        reembed_called = threading.Event()

        def _reembed_should_not_run(provider, text, *, embedding_purpose="query"):
            reembed_called.set()
            return query_vec

        try:
            with caplog.at_level(
                logging.WARNING, logger="code_indexer.storage.filesystem_vector_store"
            ):
                elapsed, results = run_sampled_search(
                    store,
                    query_vec,
                    fake_provider,
                    executor,
                    reembed_side_effect=_reembed_should_not_run,
                )
            slipped_through = reembed_called.wait(timeout=GRACE_WINDOW_SECS)
        finally:
            restore_gate(gate, drained)
            executor.shutdown(wait=True)

        assert isinstance(results, list)
        assert elapsed < PROMPT_RETURN_CEILING_SECS, (
            f"search() took {elapsed:.3f}s while the audit was gate-skipped"
        )
        assert not slipped_through, (
            "audit re-embed call ran even though the gate had zero spare "
            "capacity -- capacity is not actually bounded"
        )
        assert any(
            "capacity exhausted" in rec.message.lower() for rec in caplog.records
        ), (
            "expected an observable WARNING log when the audit is skipped "
            f"due to exhausted capacity; got: {[r.message for r in caplog.records]}"
        )

    def test_gate_is_released_after_busy_audit_completes_so_later_audit_still_runs(
        self, tiny_fsv_store
    ):
        store, vecs = tiny_fsv_store
        query_vec = vecs[0]
        fake_provider = make_fake_provider(query_vec)
        executor = ThreadPoolExecutor(max_workers=EXECUTOR_WORKERS)

        gate = fvs._deep_fidelity_audit_gate
        # Drain to zero then restore, simulating a busy slot that has just
        # freed up ("finally: gate.release()" already ran).
        drained = drain_gate(gate)
        restore_gate(gate, drained)

        reembed_fn, reembed_started, reembed_finished = make_event_signaling_reembed(
            query_vec, sleep_secs=REEMBED_SLEEP_SECS
        )
        hnsw_fn, hnsw_done, calls = make_counting_hnsw_query()

        try:
            # NOTE: the event waits below must stay INSIDE the patch context
            # -- the audit's second HNSW search runs on a background thread
            # (the dedicated audit executor), so exiting the patches before
            # it completes would race an unpatched HNSWIndexManager.query.
            with patched_audit_sampled_search(query_vec, reembed_fn, hnsw_fn):
                results = store.search(
                    query="hello",
                    embedding_provider=fake_provider,
                    collection_name=COLLECTION_NAME,
                    limit=SEARCH_LIMIT,
                    parallel_executor=executor,
                )
                assert isinstance(results, list)
                assert reembed_started.wait(timeout=EVENT_WAIT_TIMEOUT_SECS), (
                    "audit's re-embed call never started after gate release"
                )
                assert reembed_finished.wait(timeout=EVENT_WAIT_TIMEOUT_SECS), (
                    "audit's re-embed call never completed"
                )
                assert hnsw_done.wait(timeout=EVENT_WAIT_TIMEOUT_SECS), (
                    "audit did not perform its own second HNSW search after "
                    f"gate release (got {calls['n']} total HNSW query calls)"
                )
        finally:
            executor.shutdown(wait=True)

        assert_gate_at_full_capacity(gate, fvs._DEEP_FIDELITY_AUDIT_GATE_CAPACITY)
