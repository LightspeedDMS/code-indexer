"""Bug #1822 follow-up (a): the deep-fidelity audit dispatch in
FilesystemVectorStore.search() must never silently swallow a failure.

Two swallow sites (beyond the capacity-exhausted skip covered by
test_deep_fidelity_audit_bounded_capacity_1822.py) must emit an observable
WARNING log instead of a bare ``except Exception: pass``:

1. a worker exception during audit execution (the ``_ctx.run(...)`` call
   inside the background dispatch closure raising), and
2. a rejected ``executor.submit()`` call (e.g. a shutting-down executor).

Fail-open is CORRECT here -- the audit must never break search() -- but it
must be observable, so a persistently broken audit shows up in log audits
instead of vanishing.

See ``_deep_fidelity_audit_test_support_1822.py`` for shared fixtures and
the full patching-pattern rationale.

RED evidence (pre-fix code, recorded 2026-09-08): both tests fail at first
attribute access on ``fvs._deep_fidelity_audit_gate`` /
``fvs._DEEP_FIDELITY_AUDIT_GATE_CAPACITY`` with:
    AttributeError: module 'code_indexer.storage.filesystem_vector_store'
    has no attribute '_deep_fidelity_audit_gate'
(the pre-fix dispatch code has no gate at all, so there is nothing to
release/verify, and the pre-fix ``except Exception: pass`` sites emit no
log record for either scenario below).

Boundary note (worker-exception test): ``_run_deep_fidelity_audit``
(embedding_cache_audit.py) is ALREADY fully fail-open internally -- per its
own module docstring, "Fail-open: any exception inside this function is
caught and logged at WARNING" (its own distinct WARNING message). So no
exception from ITS internal collaborators can ever reach search()'s own
dispatch wrapper -- that wrapper's ``except Exception`` is a genuinely
SEPARATE, outer safety net (e.g. a bug in the dispatch closure or in
``contextvars.Context.run`` itself). This test therefore replaces the
module-level ``_run_deep_fidelity_audit`` reference that ``search()`` calls
through -- the exact seam between the dispatch wrapper (code under test)
and the audit implementation (an independently-tested collaborator, see
tests/unit/storage/test_filesystem_vector_store_audit_1110.py) -- rather
than mocking the dispatch wrapper itself.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from code_indexer.storage import filesystem_vector_store as fvs

from _deep_fidelity_audit_test_support_1822 import (
    COLLECTION_NAME,
    EVENT_WAIT_TIMEOUT_SECS,
    EXECUTOR_WORKERS,
    SEARCH_LIMIT,
    assert_gate_at_full_capacity,
    assert_log_contains,
    make_fake_provider,
    make_sampled_on_mode_coalesced,
    wait_for_log_containing,
)

# tiny_fsv_store is a pytest fixture provided by conftest.py in this same
# directory -- deliberately NOT imported here (see conftest.py's docstring
# on that fixture for why: importing a fixture function and using its name
# as a test-parameter reads to ruff as an unused import shadowed by that
# parameter).


class TestDeepFidelityAuditFailOpenIsObservable:
    """Both remaining fail-open swallow sites must log a WARNING and still
    release the gate."""

    def test_worker_exception_is_logged_and_search_still_returns(
        self, tiny_fsv_store, caplog
    ):
        store, vecs = tiny_fsv_store
        query_vec = vecs[0]
        fake_provider = make_fake_provider(query_vec)
        executor = ThreadPoolExecutor(max_workers=EXECUTOR_WORKERS)
        worker_ran = threading.Event()

        def _raising_audit(**kwargs):
            worker_ran.set()
            raise RuntimeError("synthetic audit worker failure (test)")

        try:
            with (
                patch(
                    "code_indexer.storage.filesystem_vector_store.coalesced_query_embedding",
                    side_effect=make_sampled_on_mode_coalesced(query_vec),
                ),
                patch(
                    "code_indexer.storage.filesystem_vector_store._run_deep_fidelity_audit",
                    side_effect=_raising_audit,
                ),
                caplog.at_level(
                    logging.WARNING,
                    logger="code_indexer.storage.filesystem_vector_store",
                ),
            ):
                results = store.search(
                    query="hello",
                    embedding_provider=fake_provider,
                    collection_name=COLLECTION_NAME,
                    limit=SEARCH_LIMIT,
                    parallel_executor=executor,
                )
                assert isinstance(results, list)
                assert worker_ran.wait(timeout=EVENT_WAIT_TIMEOUT_SECS), (
                    "the patched raising audit worker never ran"
                )
                wait_for_log_containing(caplog, ["audit", "fail"])
        finally:
            executor.shutdown(wait=True)

        assert_log_contains(caplog, ["audit", "fail"])
        assert_gate_at_full_capacity(
            fvs._deep_fidelity_audit_gate, fvs._DEEP_FIDELITY_AUDIT_GATE_CAPACITY
        )

    def test_submit_rejection_is_logged_and_search_still_returns(
        self, tiny_fsv_store, caplog, monkeypatch
    ):
        store, vecs = tiny_fsv_store
        query_vec = vecs[0]
        fake_provider = make_fake_provider(query_vec)
        executor = ThreadPoolExecutor(max_workers=EXECUTOR_WORKERS)

        # A separate, already-shut-down executor so submit() raises
        # RuntimeError -- WITHOUT touching the real module-level audit
        # executor other tests depend on.
        broken_audit_executor = ThreadPoolExecutor(max_workers=1)
        broken_audit_executor.shutdown(wait=True)
        monkeypatch.setattr(fvs, "_deep_fidelity_audit_executor", broken_audit_executor)

        try:
            with (
                patch(
                    "code_indexer.storage.filesystem_vector_store.coalesced_query_embedding",
                    side_effect=make_sampled_on_mode_coalesced(query_vec),
                ),
                caplog.at_level(
                    logging.WARNING,
                    logger="code_indexer.storage.filesystem_vector_store",
                ),
            ):
                results = store.search(
                    query="hello",
                    embedding_provider=fake_provider,
                    collection_name=COLLECTION_NAME,
                    limit=SEARCH_LIMIT,
                    parallel_executor=executor,
                )
                assert isinstance(results, list)
        finally:
            executor.shutdown(wait=True)

        assert_log_contains(caplog, ["executor unavailable"])
        assert_gate_at_full_capacity(
            fvs._deep_fidelity_audit_gate, fvs._DEEP_FIDELITY_AUDIT_GATE_CAPACITY
        )
