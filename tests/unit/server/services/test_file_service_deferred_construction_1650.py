"""Bug #1650 remediation: FileListingService.__init__ must be cheap too.

While verifying the coordinator's literal acceptance-test requirement for
Bug #1650 ("import a real MCP handler module -> zero bgm-worker/
bgm-temporal-worker threads spawned, zero golden-repo SQLite loads"), a
SECOND, independent occurrence of the exact same anti-pattern was found:
`server/app.py` line 134 does `from .services.file_service import
file_service as file_service` (a plain module-level import, unaffected by
app.py's own #1638 lazy-init mechanism, which only defers the specific
names listed in `_LAZY_INIT_ATTRS`). `file_service.py`'s module-level
`file_service = FileListingService()` statement then runs unconditionally,
and `FileListingService.__init__` (mirroring GitOperationsService's
original bug) eagerly constructs its OWN, entirely separate
ActivatedRepoManager -> GoldenRepoManager -> SQLite golden-repo load,
spawning its own bgm-worker/bgm-temporal-worker thread pair.

Traced empirically: importing `code_indexer.server.mcp.handlers.search` in
a fresh interpreter produced TWO independent ActivatedRepoManager
constructions and FOUR bgm threads -- one pair from git_operations_service.py
(fixed by the Option A remediation) and one pair from file_service.py (fixed
here). Fixing only one of the two leaves the literal "zero side effects"
acceptance criterion unattainable.

Patch target note: verified empirically that
`code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager`
(the source module) is the correct interception point --
FileListingService.__init__ imports ActivatedRepoManager LOCALLY (same
"avoid circular imports" reasoning as the original eager code), not as a
module-level binding in file_service.py.

Companion file test_file_service_setter_compat_1650.py covers the
setter-backward-compatibility regression tests.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent.parent / "src")
SUBPROCESS_TIMEOUT_SECONDS = 30


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
    constructing FileListingService.
    """

    def test_construction_does_not_call_activated_repo_manager_constructor(
        self, mock_activated_repo_manager_cls
    ) -> None:
        from code_indexer.server.services.file_service import FileListingService

        FileListingService()
        assert mock_activated_repo_manager_cls.call_count == 0, (
            "BUG #1650 REMEDIATION REGRESSION: FileListingService.__init__ "
            "must not construct ActivatedRepoManager eagerly -- it should "
            f"be deferred to first real access. call_count={mock_activated_repo_manager_cls.call_count}"
        )

    def test_module_level_singleton_construction_is_now_cheap(self) -> None:
        """The module-level `file_service = FileListingService()` singleton
        statement (file_service.py's own module scope) must not construct
        ActivatedRepoManager either, since it runs unconditionally whenever
        file_service.py is imported.

        Runs in a FRESH SUBPROCESS (mirroring
        test_git_operations_service_lazy_init_1650.py's established
        pattern) instead of importlib.reload()-ing the real, shared
        file_service module in-process -- reload would mutate a module
        object many other tests in this session import and rely on.
        """
        script = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import threading; "
            "before = {t.name for t in threading.enumerate()}; "
            "import code_indexer.server.services.file_service; "
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
            "BUG #1650 REMEDIATION REGRESSION: importing file_service.py "
            "(running its module-level `file_service = FileListingService()` "
            "statement) spawned background worker threads as an import-time "
            f"side effect. Subprocess output: {result.stdout!r}"
        )

    def test_first_access_constructs_activated_repo_manager_exactly_once(
        self, mock_activated_repo_manager_cls
    ) -> None:
        from code_indexer.server.services.file_service import FileListingService

        mock_activated_repo_manager_cls.return_value = "constructed-instance"
        service = FileListingService()
        first = service.activated_repo_manager
        second = service.activated_repo_manager

        assert mock_activated_repo_manager_cls.call_count == 1, (
            "ActivatedRepoManager must be constructed exactly once, lazily, "
            f"on first real access. call_count={mock_activated_repo_manager_cls.call_count}"
        )
        assert first == "constructed-instance"
        assert first is second
