"""perf regression guard: lifespan must wire a shared, long-lived query executor.

The server query path (FilesystemVectorStore.search) used to create a fresh
ThreadPoolExecutor PER REQUEST for the embed||index-load fan-out. Under
concurrent load the per-request create/destroy churn serialized on CPython's
process-wide _global_shutdown_lock (71% of worker-thread py-spy samples in
`submit`). The fix creates ONE long-lived ThreadPoolExecutor at startup,
exposes it on app.state.query_executor (so SemanticSearchService can inject it
into search()), and shuts it down cleanly on lifespan shutdown.

Mirrors the Bug #1070 _xray_executor wiring guard.

Source-text checks (exact patterns):
- "_query_executor = ThreadPoolExecutor(" — executor creation
- "query_executor_pool_size"             — config field drives max_workers
- "app.state.query_executor = _query_executor" — exposed on app.state
- "_query_executor.shutdown("            — graceful shutdown on exit

Source-order checks:
- creation appears BEFORE the lifespan `yield`
- shutdown appears AFTER the lifespan `yield`

All tests MUST fail before the fix and pass after.
"""

from __future__ import annotations

from pathlib import Path

# tests/unit/server/startup/ -> tests/unit/server/ -> tests/unit/ -> tests/ -> repo root
_PARENTS_TO_REPO_ROOT = 4

_REPO_ROOT = Path(__file__).resolve().parents[_PARENTS_TO_REPO_ROOT]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


def _source() -> str:
    return _LIFESPAN_PATH.read_text()


class TestLifespanQueryExecutorWiringSource:
    def test_query_executor_creation_statement_present(self):
        source = _source()
        assert "_query_executor = ThreadPoolExecutor(" in source, (
            "lifespan.py must create '_query_executor = ThreadPoolExecutor(...)'."
        )

    def test_query_executor_pool_size_config_field_referenced(self):
        source = _source()
        assert "query_executor_pool_size" in source, (
            "lifespan.py must size the query executor from 'query_executor_pool_size'."
        )

    def test_query_executor_exposed_on_app_state(self):
        source = _source()
        assert "app.state.query_executor = _query_executor" in source, (
            "lifespan.py must expose 'app.state.query_executor = _query_executor'."
        )

    def test_query_executor_shutdown_call_present(self):
        source = _source()
        assert "_query_executor.shutdown(" in source, (
            "lifespan.py must call '_query_executor.shutdown(' on server shutdown."
        )


class TestLifespanQueryExecutorWiringOrder:
    def test_creation_before_yield_and_shutdown_after(self):
        source = _source()
        create_idx = source.find("_query_executor = ThreadPoolExecutor(")
        yield_idx = source.find("yield  # Server is now running")
        shutdown_idx = source.find("_query_executor.shutdown(")

        assert create_idx != -1, "query executor creation statement missing"
        assert yield_idx != -1, "lifespan yield marker missing"
        assert shutdown_idx != -1, "query executor shutdown statement missing"

        assert create_idx < yield_idx, (
            "Query executor must be CREATED before the lifespan yield (startup)."
        )
        assert shutdown_idx > yield_idx, (
            "Query executor must be SHUT DOWN after the lifespan yield (shutdown)."
        )
