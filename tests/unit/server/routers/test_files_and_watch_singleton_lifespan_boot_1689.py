"""Bug #1689 regression: real server startup must survive the lazy
`FileCRUDService.activated_repo_manager` property and the lazy
`AutoWatchManager` timeout-checker thread.

This is the single most important check for #1689, per the task's own
documented lesson from the sibling issue #1686: a near-identical
lazy-singleton fix (routers/diagnostics.py) was REJECTED in its first
round because it broke real server startup -- `startup/lifespan.py` did a
bare-name `from module import name` against a singleton the fix had bound
to `None`, and `_backend = ...` raised `AttributeError:
'NoneType' object has no attribute '_backend'` with no enclosing
try/except, aborting the entire boot.

For #1689, `startup/service_init.py`'s Story #197 AC1/AC4 code does:

    from code_indexer.server.services.file_crud_service import file_crud_service
    file_crud_service.register_write_exception("cidx-meta-global", cidx_meta_path)
    file_crud_service.set_golden_repos_dir(Path(golden_repo_manager.golden_repos_dir))

This is a FUNCTION-LOCAL import inside the real startup lifespan coroutine
(not a module-level `from module import name`, unlike #1686's
diagnostics_service trap), and file_crud_service.py keeps its module-level
`file_crud_service = FileCRUDService()` singleton binding EAGER (Layer-2-only
fix, mirroring file_service.py's Bug #1650 remediation) -- so there is no
None-sentinel to trip over. This test proves that assumption is correct by
booting the REAL app lifespan (mirroring test_git_cat_endpoint.py's
established `from code_indexer.server.app import app` +
`with TestClient(app) as client:` pattern) and confirming the write
exception really got registered against the singleton during boot, not
just that boot didn't crash.
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.services.file_crud_service import file_crud_service


class TestRealLifespanBootSurvivesLazyFileCrudServiceSingleton:
    """FileCRUDService.activated_repo_manager becoming a lazy property
    must not break the real server-startup wiring."""

    def test_real_lifespan_boot_registers_cidx_meta_write_exception(self) -> None:
        """Booting the real app lifespan must not raise (the exact class
        of failure #1686 introduced and #1689 must not repeat), AND real
        startup must actually run service_init.py's Story #197 AC1/AC4
        write-exception registration against the file_crud_service
        singleton -- not just avoid crashing. This proves the lazy
        activated_repo_manager property change is fully transparent to
        real production startup code, matching pre-fix behavior exactly.
        """
        with TestClient(app):
            assert file_crud_service.is_write_exception("cidx-meta-global"), (
                "BUG #1689 REGRESSION: real server startup must register "
                "'cidx-meta-global' as a write exception on the "
                "file_crud_service singleton (Story #197 AC1/AC4), "
                "regardless of activated_repo_manager now being a lazy "
                "property instead of a plain eager attribute."
            )


class TestRealLifespanBootDoesNotEagerlyTriggerAutoWatch:
    """AutoWatchManager's lazily-started checker thread must not be
    spuriously started merely by server startup -- only a real
    start_watch() call (a file write against an activated repo) should
    start it."""

    def test_boot_spawns_zero_new_checker_threads(self) -> None:
        """Track actual Thread OBJECTS (not names): the checker thread
        uses a fixed, non-unique name, and other tests running earlier in
        this same pytest process may have already started ONE against the
        shared `auto_watch_manager` module singleton -- a name-based check
        would give a false result either way. Object identity proves this
        SPECIFIC boot did not spawn a new one, independent of prior state.
        """
        before = {
            t for t in threading.enumerate() if t.name == "AutoWatchTimeoutChecker"
        }
        with TestClient(app):
            after = {
                t for t in threading.enumerate() if t.name == "AutoWatchTimeoutChecker"
            }
        new_threads = after - before

        assert new_threads == set(), (
            "BUG #1689 REGRESSION: real server startup must not itself "
            "trigger AutoWatchManager.start_watch() (which would lazily "
            "start the checker thread) -- watch mode only activates on a "
            f"real file write against an activated repository. New "
            f"threads spawned by boot: {new_threads}"
        )
