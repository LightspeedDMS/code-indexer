"""Bug #1070 regression guard: lifespan must wire a dedicated xray_executor.

Bug #1070: handle_xray_search and handle_xray_explore submit to BackgroundJobManager's
5-worker pool, causing starvation and 504s. The fix requires:
1. A dedicated ThreadPoolExecutor for xray (xray_max_concurrent_jobs = 20 workers).
2. lifespan.py creates it and injects it via set_xray_executor() so the handlers
   dispatch xray compute there instead of the shared BJM pool.

Source-text checks (exact patterns):
- "_xray_executor = ThreadPoolExecutor(" — executor creation
- "xray_max_concurrent_jobs" — config field drives max_workers
- "set_xray_executor(_xray_executor)" — injection call
- "_xray_executor.shutdown(" — graceful shutdown on exit

All tests MUST fail before the Bug #1070 fix and pass after.
"""

from __future__ import annotations

from pathlib import Path

# Named constant: distance from this test file to the repository root.
# Path: tests/unit/server/startup/ -> tests/unit/server/ -> tests/unit/ -> tests/ -> repo root
_PARENTS_TO_REPO_ROOT = 4

_REPO_ROOT = Path(__file__).resolve().parents[_PARENTS_TO_REPO_ROOT]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


def _source() -> str:
    return _LIFESPAN_PATH.read_text()


class TestLifespanXrayExecutorWiringSource:
    """Source-text guard: lifespan.py must create and inject a dedicated xray_executor."""

    def test_xray_executor_creation_statement_present(self):
        """lifespan.py must contain '_xray_executor = ThreadPoolExecutor('.

        Fails before fix (no dedicated xray executor); passes after.
        """
        source = _source()

        assert "_xray_executor = ThreadPoolExecutor(" in source, (
            "Bug #1070: lifespan.py does not contain "
            "'_xray_executor = ThreadPoolExecutor('.\n"
            "Add: _xray_executor = ThreadPoolExecutor(max_workers=xray_max_concurrent_jobs)"
        )

    def test_xray_max_concurrent_jobs_config_field_referenced(self):
        """lifespan.py must reference 'xray_max_concurrent_jobs' to size the executor.

        Fails before fix; passes after.
        """
        source = _source()

        assert "xray_max_concurrent_jobs" in source, (
            "Bug #1070: lifespan.py does not reference 'xray_max_concurrent_jobs'.\n"
            "The max_workers must be driven by BackgroundJobsConfig.xray_max_concurrent_jobs."
        )

    def test_set_xray_executor_injection_call_present(self):
        """lifespan.py must call 'set_xray_executor(_xray_executor)' exactly.

        Fails before fix; passes after.
        """
        source = _source()

        assert "set_xray_executor(_xray_executor)" in source, (
            "Bug #1070: lifespan.py does not call 'set_xray_executor(_xray_executor)'.\n"
            "Add:\n"
            "  from code_indexer.server.mcp.handlers.xray import set_xray_executor\n"
            "  set_xray_executor(_xray_executor)"
        )

    def test_xray_executor_shutdown_call_present(self):
        """lifespan.py must call '_xray_executor.shutdown(' on server shutdown.

        Fails before fix; passes after.
        """
        source = _source()

        assert "_xray_executor.shutdown(" in source, (
            "Bug #1070: lifespan.py does not call '_xray_executor.shutdown('.\n"
            "Add _xray_executor.shutdown(wait=False) near _mcp_executor.shutdown."
        )
