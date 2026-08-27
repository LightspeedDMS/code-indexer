"""Bug #1689 remediation: FileCRUDService.__init__ must be cheap.

`FileCRUDService.__init__` eagerly constructs a real `ActivatedRepoManager`
(-> GoldenRepoManager -> SQLite golden-repo load, spawning
bgm-worker/bgm-temporal-worker threads per the documented Bug #1650
measurement), and the module-level statement
`file_crud_service = FileCRUDService()` at the bottom of
file_crud_service.py runs that constructor unconditionally at import
time -- so any bare or transitive import of the module paid the full
construction cost as a side effect, with no explicit opt-in. This is the
exact Bug #1638/#1650 anti-pattern documented in CLAUDE.md's "Module-Level
Service Singletons Must Be Lazy (PEP 562)" section, filed as its own issue
(#1689) after being spotted during #1683's round-4 review.

Consumer audit (exhaustive grep across src/ and tests/, see issue #1689
work):
  - `mcp/handlers/files.py`: every reference is a FUNCTION-LOCAL
    `from ...file_crud_service import file_crud_service` inside handler
    functions (handle_create_file, handle_edit_file, handle_delete_file,
    _prepare_write_mode_context, handle_enter_write_mode,
    _start_auto_watch_if_needed) -- never module-level.
  - `routers/files.py`: module-level import, but only of the CLASS
    (`FileCRUDService`) and an exception (`HashMismatchError`) -- NOT the
    singleton instance.
  - `startup/service_init.py`: imports the singleton instance, but
    FUNCTION-LOCAL, inside the real startup lifespan coroutine (the
    legitimate place for real construction to happen -- at server startup,
    not at bare import time).
There is therefore NO module-level `from module import file_crud_service`
production consumer anywhere -- an even cleaner case than
git_operations_service.py's Bug #1650 fix (which kept a defense-in-depth
module-level `__getattr__` because 5 real consumers DID bind the name at
module scope). Given that, and mirroring `file_service.py`'s Bug #1650
remediation (which also has zero such consumers), the fix here is
Layer-2-only: make `FileCRUDService.__init__` itself cheap by deferring
`ActivatedRepoManager` construction to a lazy `activated_repo_manager`
property, and keep the module-level singleton binding eager (harmless once
construction is side-effect-free).

Patch target note: `ActivatedRepoManager` is imported LOCALLY inside
`FileCRUDService.__init__` (not module-level in file_crud_service.py), so
the correct patch target is the SOURCE module
(`code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager`),
matching the established pattern in
test_file_service_deferred_construction_1650.py and
test_git_operations_service_deferred_construction_1650.py.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent.parent / "src")
SUBPROCESS_TIMEOUT_SECONDS = 30
THREAD_JOIN_TIMEOUT_SECONDS = 10


@pytest.fixture
def mock_activated_repo_manager_cls():
    with patch(
        "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
    ) as mock_cls:
        yield mock_cls


class TestInitDoesNotEagerlyConstructActivatedRepoManager:
    """__init__ must not build ActivatedRepoManager (and therefore not
    GoldenRepoManager, not the SQLite golden-repo load, not the
    bgm-worker/bgm-temporal-worker threads) as a side effect of merely
    constructing FileCRUDService.
    """

    def test_construction_does_not_call_activated_repo_manager_constructor(
        self, mock_activated_repo_manager_cls
    ) -> None:
        from code_indexer.server.services.file_crud_service import FileCRUDService

        FileCRUDService()
        assert mock_activated_repo_manager_cls.call_count == 0, (
            "BUG #1689 REGRESSION: FileCRUDService.__init__ must not "
            "construct ActivatedRepoManager eagerly -- it should be "
            "deferred to first real access. "
            f"call_count={mock_activated_repo_manager_cls.call_count}"
        )

    def test_module_level_singleton_construction_is_now_cheap(self) -> None:
        """The module-level `file_crud_service = FileCRUDService()`
        singleton statement must not construct ActivatedRepoManager
        either, since it runs unconditionally whenever
        file_crud_service.py is imported.

        Runs in a FRESH SUBPROCESS (mirrors
        test_file_service_deferred_construction_1650.py's established
        pattern) instead of importlib.reload()-ing the real, shared
        module in-process -- reload would mutate a module object many
        other tests in this session import and rely on.
        """
        script = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import threading; "
            "before = {t.name for t in threading.enumerate()}; "
            "import code_indexer.server.services.file_crud_service; "
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
            "BUG #1689 REGRESSION: importing file_crud_service.py (running "
            "its module-level `file_crud_service = FileCRUDService()` "
            "statement) spawned background worker threads as an "
            f"import-time side effect. Subprocess output: {result.stdout!r}"
        )

    def test_first_access_constructs_activated_repo_manager_exactly_once(
        self, mock_activated_repo_manager_cls
    ) -> None:
        from code_indexer.server.services.file_crud_service import FileCRUDService

        mock_activated_repo_manager_cls.return_value = "constructed-instance"
        service = FileCRUDService()
        first = service.activated_repo_manager
        second = service.activated_repo_manager

        assert mock_activated_repo_manager_cls.call_count == 1, (
            "ActivatedRepoManager must be constructed exactly once, "
            "lazily, on first real access. "
            f"call_count={mock_activated_repo_manager_cls.call_count}"
        )
        assert first == "constructed-instance"
        assert first is second


class TestActivatedRepoManagerReentrancyDoesNotRecurse:
    """The activated_repo_manager lazy property's RLock stops cross-thread
    deadlock but NOT same-thread re-entrant recursion -- on re-entry the
    double-checked `is None` test is still True (the assignment happens
    only after the constructor returns), so an unguarded re-entrant call
    during construction would construct AGAIN. Mirrors
    test_file_service_deferred_construction_1650.py's identical test
    exactly, using a background thread with a bounded join(timeout=...).
    """

    def test_reentrant_access_during_construction_does_not_recurse(self) -> None:
        from code_indexer.server.services.file_crud_service import FileCRUDService

        service = FileCRUDService()

        construction_count = {"n": 0}
        reentrant_outcome: dict = {}

        class ReentrantARM:
            def __init__(self, *args, **kwargs):
                construction_count["n"] += 1
                if construction_count["n"] == 1:
                    # Re-entrant probe from WITHIN construction, same thread.
                    try:
                        reentrant_outcome["value"] = service.activated_repo_manager
                    except Exception as e:  # noqa: BLE001 - captured for assertion
                        reentrant_outcome["exception"] = e

        result: dict = {}

        def worker() -> None:
            try:
                with patch(
                    "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager",
                    ReentrantARM,
                ):
                    result["value"] = service.activated_repo_manager
            except Exception as e:  # noqa: BLE001 - captured for assertion
                result["exception"] = e

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

        assert not t.is_alive(), (
            "REGRESSION: re-entrant access during construction hung "
            "(unbounded recursion or deadlock)."
        )
        assert construction_count["n"] == 1, (
            "BUG #1689 REGRESSION: the constructor must run EXACTLY ONCE "
            "-- a re-entrant call arriving mid-construction must not "
            "trigger a second/recursive construction. "
            f"construction_count={construction_count['n']}"
        )
        assert "exception" in reentrant_outcome, (
            "The re-entrant call must raise (matching pre-fix unbound "
            f"semantics), not silently return a value. Got: {reentrant_outcome}"
        )
        assert "value" in result, f"outer call must succeed: {result}"
        assert "exception" not in result, (
            f"outer (original) call must not raise: {result}"
        )


class TestActivatedRepoManagerSetterStillWorksForTestPatching:
    """Several existing test files assign `service.activated_repo_manager`
    directly (test_file_crud_unicode_bom.py), and some construct the
    service via `FileCRUDService.__new__(FileCRUDService)` (bypassing
    __init__ entirely) before assigning (test_file_crud_write_mode.py).
    Both patterns must be unaffected by converting activated_repo_manager
    from a plain instance attribute into a lazy property with a setter.
    """

    def test_direct_assignment_and_readback(
        self, mock_activated_repo_manager_cls
    ) -> None:
        from code_indexer.server.services.file_crud_service import FileCRUDService

        service = FileCRUDService()

        sentinel = object()
        service.activated_repo_manager = sentinel
        assert service.activated_repo_manager is sentinel

        service.activated_repo_manager = None
        mock_activated_repo_manager_cls.return_value = "fresh-instance"
        assert service.activated_repo_manager == "fresh-instance"

    def test_new_bypass_then_direct_assignment(self) -> None:
        """Mirrors test_file_crud_unicode_bom.py's fixture pattern:
        FileCRUDService.__new__(FileCRUDService) bypasses __init__
        entirely, then the test assigns activated_repo_manager directly.
        """
        from code_indexer.server.services.file_crud_service import FileCRUDService

        service = FileCRUDService.__new__(FileCRUDService)
        mock_arm = MagicMock()
        service.activated_repo_manager = mock_arm
        assert service.activated_repo_manager is mock_arm
