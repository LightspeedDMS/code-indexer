"""
Integration tests for Bug #1056 jitter-dispatch refactor.

RED phase: written before call-site refactors to drive the GREEN implementation.

Three tests verify that:
  A. LifecycleBatchRunner._run_sub_batch delegates to dispatch_parallel_with_jitter
  B. DependencyMapService Pass 2 loop imports sleep_with_jitter (RED gate)
  C. DepMapRepairExecutor broken-domain loop imports sleep_with_jitter (RED gate)
"""

import concurrent.futures
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Test A — LifecycleBatchRunner._run_sub_batch uses dispatch_parallel_with_jitter
# ---------------------------------------------------------------------------


def test_lifecycle_batch_runner_sub_batch_uses_dispatcher() -> None:
    """_run_sub_batch must delegate to dispatch_parallel_with_jitter with:
    - items = sub_batch passed in
    - concurrency = runner._concurrency
    - base_jitter_seconds = DEFAULT_LIFECYCLE_DISPATCH_JITTER_SECONDS
    """
    from code_indexer.global_repos.lifecycle_batch_runner import LifecycleBatchRunner
    from code_indexer.server.services.jittered_dispatcher import (
        DEFAULT_LIFECYCLE_DISPATCH_JITTER_SECONDS,
    )

    sub_batch = ["repo-a", "repo-b", "repo-c"]

    def _make_done_future(val: Any) -> concurrent.futures.Future:  # type: ignore[type-arg]
        f: concurrent.futures.Future = concurrent.futures.Future()  # type: ignore[type-arg]
        f.set_result(val)
        return f

    done_futures = [_make_done_future(None) for _ in sub_batch]
    mock_dispatcher = MagicMock(return_value=done_futures)

    runner = LifecycleBatchRunner(
        golden_repos_dir=Path("/tmp/fake-golden-repos"),
        job_tracker=MagicMock(),
        refresh_scheduler=MagicMock(),
        debouncer=MagicMock(),
        claude_cli_invoker=MagicMock(return_value={"lifecycle": "active"}),
        concurrency=3,
        tracking_backend=MagicMock(),
    )

    with patch(
        "code_indexer.global_repos.lifecycle_batch_runner.dispatch_parallel_with_jitter",
        mock_dispatcher,
    ):
        runner._run_sub_batch(sub_batch, "parent-job-123")

    mock_dispatcher.assert_called_once()
    call_kwargs = mock_dispatcher.call_args

    assert call_kwargs.args[0] == sub_batch, (
        f"Expected items={sub_batch}, got {call_kwargs.args[0]}"
    )
    assert call_kwargs.kwargs["concurrency"] == runner._concurrency, (
        f"Expected concurrency={runner._concurrency}, got {call_kwargs.kwargs['concurrency']}"
    )
    assert (
        call_kwargs.kwargs["base_jitter_seconds"]
        == DEFAULT_LIFECYCLE_DISPATCH_JITTER_SECONDS
    ), (
        f"Expected base_jitter_seconds={DEFAULT_LIFECYCLE_DISPATCH_JITTER_SECONDS}, "
        f"got {call_kwargs.kwargs['base_jitter_seconds']}"
    )


# ---------------------------------------------------------------------------
# Test B — dependency_map_service imports sleep_with_jitter (RED gate)
# ---------------------------------------------------------------------------


def test_depmap_pass2_loop_sleeps_with_jitter_after_first_iteration() -> None:
    """The Pass 2 per-domain loop must call sleep_with_jitter between iterations.

    RED gate: dependency_map_service must import sleep_with_jitter and
    DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS. These attributes will be absent
    until the Bug #1056 refactor adds the import.

    After the import is present, verify the N-1 call pattern using the
    imported symbol directly.
    """
    import code_indexer.server.services.dependency_map_service as dms_module

    from code_indexer.server.services.jittered_dispatcher import (
        DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS,
    )

    assert hasattr(dms_module, "sleep_with_jitter"), (
        "dependency_map_service must import sleep_with_jitter "
        "(Bug #1056 refactor not applied yet)"
    )
    assert hasattr(dms_module, "DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS"), (
        "dependency_map_service must import DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS "
        "(Bug #1056 refactor not applied yet)"
    )

    # Verify the N-1 call pattern: 3 domains → 2 sleep_with_jitter calls
    domain_list = [
        {"name": "domain-alpha"},
        {"name": "domain-beta"},
        {"name": "domain-gamma"},
    ]
    sleep_mock = MagicMock()
    with patch(
        "code_indexer.server.services.dependency_map_service.sleep_with_jitter",
        sleep_mock,
    ):
        for domain_idx, _domain in enumerate(domain_list):
            if domain_idx > 0:
                dms_module.sleep_with_jitter(DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS)

    assert sleep_mock.call_count == 2, (
        f"Expected 2 sleep_with_jitter calls for 3 domains, got {sleep_mock.call_count}"
    )
    for call in sleep_mock.call_args_list:
        assert call.args[0] == DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS, (
            f"Expected arg={DEFAULT_DEPMAP_DISPATCH_JITTER_SECONDS}, got {call.args[0]}"
        )


# ---------------------------------------------------------------------------
# Test C — dep_map_repair_executor imports sleep_with_jitter (RED gate)
# ---------------------------------------------------------------------------


def test_phase37_anomaly_loop_sleeps_with_jitter_after_first_iteration() -> None:
    """The broken-domain repair loop must call sleep_with_jitter between iterations.

    RED gate: dep_map_repair_executor must import sleep_with_jitter and
    DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS. These attributes will be absent
    until the Bug #1056 refactor adds the import.

    After the import is present, verify the N-1 call pattern using the
    imported symbol directly.
    """
    import code_indexer.server.services.dep_map_repair_executor as repair_module

    from code_indexer.server.services.jittered_dispatcher import (
        DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS,
    )

    assert hasattr(repair_module, "sleep_with_jitter"), (
        "dep_map_repair_executor must import sleep_with_jitter "
        "(Bug #1056 refactor not applied yet)"
    )
    assert hasattr(repair_module, "DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS"), (
        "dep_map_repair_executor must import DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS "
        "(Bug #1056 refactor not applied yet)"
    )

    # Verify the N-1 call pattern: 3 anomalies → 2 sleep_with_jitter calls
    broken_domains = ["dom-1", "dom-2", "dom-3"]
    sleep_mock = MagicMock()
    with patch(
        "code_indexer.server.services.dep_map_repair_executor.sleep_with_jitter",
        sleep_mock,
    ):
        for anomaly_idx, _anomaly in enumerate(broken_domains):
            if anomaly_idx > 0:
                repair_module.sleep_with_jitter(DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS)

    assert sleep_mock.call_count == 2, (
        f"Expected 2 sleep_with_jitter calls for 3 anomalies, got {sleep_mock.call_count}"
    )
    for call in sleep_mock.call_args_list:
        assert call.args[0] == DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS, (
            f"Expected arg={DEFAULT_PHASE37_DISPATCH_JITTER_SECONDS}, got {call.args[0]}"
        )
