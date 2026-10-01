"""Proves Issue #1516's code-review Defect 2: `reset_global_parallel_query_
executor()` clears the global singleton reference inside its lock but calls
`shutdown(wait=False)` on the OLD instance OUTSIDE the lock. A caller that
captured a reference to that (about-to-be-old) executor via
`get_global_parallel_query_executor()` moments earlier can then call
`.submit(...)` on it concurrently with (or just after) that `shutdown()`
call, raising `RuntimeError: cannot schedule new futures after shutdown`.

Before the fix: this RuntimeError propagates straight out of
`_search_single_repository`'s submit loop, which would surface as a 500 to
a real request during a graceful lifespan shutdown -- a genuine crash risk,
not a mere log line.

After the fix: the submit loop treats this exact race as an infra-level
pool-shutdown condition -- gracefully skip the affected provider (like a
down/sin-binned provider a few lines above in the same method), log a
warning, and critically do NOT call
`ProviderHealthMonitor.record_call(..., success=False)` for it, since this
is not a genuine provider failure and must never incorrectly contribute to
sin-binning a healthy provider.

Race simulation: `semantic_query_manager.py` contains NO local
`ThreadPoolExecutor(` construction -- `_search_single_repository` obtains
its executor via `get_global_parallel_query_executor()` directly. Calling
`.shutdown(wait=True)` on the exact object that accessor returns mutates
the shared singleton itself (no `reset_global_parallel_query_executor()`
call is needed to reproduce this): the method's own subsequent internal
call to that same accessor returns the identical, now-shut-down object,
deterministically reproducing the race with no actual concurrent threads
needed.

`_search_with_provider` is deliberately left as the REAL bound method
here (not mocked): a `ThreadPoolExecutor.submit()` call on an
already-shut-down executor synchronously raises `RuntimeError` before any
task is ever queued or run, so the provider-search callable is provably
never invoked -- whether pre-fix or post-fix -- and no mock is needed to
prove that.
"""

import logging
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.query.parallel_query_executor import (
    get_global_parallel_query_executor,
    reset_global_parallel_query_executor,
)
from code_indexer.server.query.semantic_query_manager import SemanticQueryManager
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor


@pytest.fixture(autouse=True)
def reset_health_monitor():
    """Reset ProviderHealthMonitor singleton before/after each test."""
    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture(autouse=True)
def reset_shared_executor():
    """Test isolation for the shared parallel-query executor singleton."""
    reset_global_parallel_query_executor()
    yield
    reset_global_parallel_query_executor()


@pytest.fixture
def repo_path():
    path = tempfile.mkdtemp()
    yield path
    # No ignore_errors: a genuine cleanup failure must surface, not be
    # silently swallowed.
    shutil.rmtree(path)


@pytest.fixture
def manager():
    """SemanticQueryManager with mocked constructor-dependency scaffolding
    (established fixture pattern -- mirrors `test_semantic_query_manager_
    sinbin.py`'s own `manager` fixture verbatim)."""
    m = SemanticQueryManager.__new__(SemanticQueryManager)
    m.data_dir = "/fake/data"
    m.query_timeout_seconds = 30
    m.max_concurrent_queries_per_user = 5
    m.max_results_per_query = 100
    m._active_queries_per_user = {}
    m.logger = logging.getLogger(__name__)
    mock_arm = MagicMock()
    mock_arm.activated_repos_dir = "/fake/data/activated_repos"
    m.activated_repo_manager = mock_arm
    m.background_job_manager = MagicMock()
    return m


def test_shutdown_race_during_submit_does_not_raise_and_does_not_sinbin_provider(
    manager, repo_path, caplog
):
    # Simulate the race: this caller (the test) holds a reference to the
    # shared executor and shuts it down directly, exactly as would happen
    # if reset_global_parallel_query_executor() ran concurrently and this
    # code path still held the pre-reset reference.
    executor = get_global_parallel_query_executor()
    executor.shutdown(wait=True)

    health_monitor = ProviderHealthMonitor.get_instance()

    with patch.object(
        health_monitor, "record_call", wraps=health_monitor.record_call
    ) as record_call_spy:
        with caplog.at_level(logging.WARNING):
            # Must NOT raise -- this is the core assertion. Before the fix,
            # the unguarded executor.submit(...) call raises RuntimeError
            # here.
            results = manager._search_single_repository(
                repo_path=repo_path,
                repository_alias="test-repo",
                query_text="authentication",
                limit=10,
                min_score=None,
                file_extensions=None,
                query_strategy="parallel",
            )

    assert results == [], (
        "Expected no results when every provider's submit() call raced "
        f"against a pool shutdown, got: {results}"
    )
    assert record_call_spy.call_count == 0, (
        "A submit()-time pool-shutdown race must NEVER be recorded as a "
        f"provider failure (would incorrectly sin-bin a healthy provider), "
        f"got calls: {record_call_spy.call_args_list}"
    )
    assert any(
        "shutdown" in record.getMessage().lower()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ), (
        "Expected a WARNING log naming the submit()-time pool-shutdown "
        f"race; got log messages: {[r.getMessage() for r in caplog.records]}"
    )
