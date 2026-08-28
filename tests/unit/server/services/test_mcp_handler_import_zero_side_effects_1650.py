"""Bug #1650 literal acceptance test: importing a real MCP handler module
must produce ZERO import-time side effects.

This is the exact reproduction from the original bug report: "importing
`code_indexer.server.mcp.handlers.search` in a bare process ... the
golden-repo-loading and worker-thread side effects still occurred."

Code review of the first #1650 fix attempt (commit 2085fe9a, module-level
PEP 562 lazy-init only) proved this repro was UNCHANGED, pre-fix and
post-fix: byte-for-byte identical result -- 2x "Loaded N golden repos from
SQLite" and 14 threads, both times. Root
cause: `mcp.handlers.search` imports `mcp.handlers.__init__`/`_legacy.py`
(package init runs first), which bind `git_operations_service` at MODULE
SCOPE via `from ... import git_operations_service` -- PEP 562's
__getattr__ fires transparently on that statement too, so the module-level
deferral alone never prevented construction.

Tracing (stack-trace instrumentation on ActivatedRepoManager.__init__,
during this investigation) revealed the 2x/14-threads count came from TWO
INDEPENDENT sources:
  1. mcp/handlers/_legacy.py -> git_operations_service.py's __getattr__ ->
     GitOperationsService.__init__ -> (eagerly) ActivatedRepoManager()
  2. server/app.py line 134 (`from .services.file_service import
     file_service as file_service`, a plain module-level import NOT
     covered by app.py's #1638 _LAZY_INIT_ATTRS mechanism) ->
     file_service.py's module-level `file_service = FileListingService()`
     -> FileListingService.__init__ -> (eagerly) its OWN, separate
     ActivatedRepoManager()

Both are now fixed via the identical Option A pattern (defer
ActivatedRepoManager construction to first real property access instead of
__init__ time): GitOperationsService in git_operations_service.py, and
FileListingService in file_service.py. This test proves the combination
achieves the literal "zero side effects" requirement, not just a partial
reduction.
"""

import subprocess
import sys
from pathlib import Path

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent.parent / "src")
SUBPROCESS_TIMEOUT_SECONDS = 30


def test_importing_mcp_search_handler_spawns_zero_bgm_threads_and_zero_golden_repo_loads() -> (
    None
):
    script = (
        "import sys; "
        f"sys.path.insert(0, {SRC_ROOT!r}); "
        "import threading, logging; "
        "logging.basicConfig(level=logging.INFO); "
        "before = {t.name for t in threading.enumerate()}; "
        "import code_indexer.server.mcp.handlers.search; "
        "after = {t.name for t in threading.enumerate()}; "
        "new_threads = after - before; "
        "bgm_threads = [n for n in new_threads if 'bgm' in n.lower()]; "
        "print('new_bgm_threads:', bgm_threads)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    assert "new_bgm_threads: []" in result.stdout, (
        "BUG #1650 ACCEPTANCE FAILURE: importing "
        "code_indexer.server.mcp.handlers.search spawned background worker "
        f"threads as an import-time side effect. stdout: {result.stdout!r}"
    )

    assert "golden repos from SQLite" not in result.stderr, (
        "BUG #1650 ACCEPTANCE FAILURE: importing "
        "code_indexer.server.mcp.handlers.search triggered a real "
        "golden-repo SQLite load as an import-time side effect. "
        f"stderr: {result.stderr!r}"
    )
