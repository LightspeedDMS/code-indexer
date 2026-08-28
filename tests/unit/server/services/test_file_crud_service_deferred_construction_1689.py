"""Bug #1689 remediation: FileCRUDService.__init__ must be cheap.

`FileCRUDService.__init__` used to eagerly construct a real
`ActivatedRepoManager` (-> GoldenRepoManager -> SQLite golden-repo load,
spawning bgm-worker/bgm-temporal-worker threads per the documented Bug
#1650 measurement), and the module-level statement
`file_crud_service = FileCRUDService()` at the bottom of
file_crud_service.py ran that constructor unconditionally at import time
-- so any bare or transitive import of the module paid the full
construction cost as a side effect, with no explicit opt-in. This is the
exact Bug #1638/#1650 anti-pattern documented in CLAUDE.md's "Module-Level
Service Singletons Must Be Lazy (PEP 562)" section, filed as its own issue
(#1689) after being spotted during #1683's round-4 review.

Consumer audit (exhaustive grep across src/ and tests/, see issue #1689
work): there was NO module-level `from module import file_crud_service`
production consumer anywhere, so the original fix (this file's earlier
revision) deferred `ActivatedRepoManager` construction via a lazy
`activated_repo_manager` property on the instance.

Bug #1703 follow-up (THIS revision): a later fix for Bug #1692 moved
`_resolve_repo_path`'s ONLY call site off `self.activated_repo_manager`
and onto the module-level, DI/app.state-wired `_get_activated_repo_manager()`
function instead (that node-local, unpooled property instance can't see
PostgreSQL-mode activation state in cluster deployments -- see
`_get_activated_repo_manager()`'s docstring). That left the
`activated_repo_manager` property with ZERO production consumers -- a
Messi Rule 12 (anti-orphan-code) violation and a latent trap for
reintroducing #1692's exact cluster-outage defect class if a future
developer reached for it again.

Consumer audit re-run for #1703 (exhaustive grep across src/ and tests/):
confirmed zero references to `self.activated_repo_manager` /
`.activated_repo_manager` on a `FileCRUDService` instance anywhere in
production code. The only test consumers were this file's own
Bug #1689 property-mechanism tests (removed below) and one inert,
already-non-functional mock patch in
`tests/unit/server/mcp/test_write_mode_tools.py` (fixed separately in the
same commit -- that patch target was never actually consulted by
`_resolve_repo_path` after #1692 landed, so removing it changes no
tested behavior).

Given all of that, the property (and the per-instance lazy-construction
state it alone existed to support: `_activated_repo_manager_lazy`,
`_activated_repo_manager_lock`, `_arm_initializing`) was DELETED entirely,
mirroring the identical precedent set by Bug #1702 for
`routers/git.py`'s analogous module-level singleton (see
`test_git_router_uses_app_state_1702.py::TestNoModuleLevelSingletonRemains`).

This revision keeps the two tests below that protect the actual Bug #1689
regression (eager construction as an import/construction-time side
effect) -- neither of which ever depended on the property existing -- and
replaces the property-specific tests (first-access-constructs-once,
same-thread re-entrancy, setter-roundtrip-for-test-patching) with
tests asserting the orphaned property and its backing state are actually
gone, so a future re-introduction of a node-local
`self.activated_repo_manager` (the exact shape of #1692's defect) is
caught immediately.

Patch target note: `ActivatedRepoManager` is imported LOCALLY (not
module-level) wherever it is still constructed in this codebase, so the
correct patch target remains the SOURCE module
(`code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager`),
matching the established pattern in
test_file_service_deferred_construction_1650.py and
test_git_operations_service_deferred_construction_1650.py.
"""

from __future__ import annotations

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
    constructing FileCRUDService. This holds regardless of whether a lazy
    `activated_repo_manager` property exists at all (#1703 removed it) --
    what matters is that __init__ itself never touches
    ActivatedRepoManager construction.
    """

    def test_construction_does_not_call_activated_repo_manager_constructor(
        self, mock_activated_repo_manager_cls
    ) -> None:
        from code_indexer.server.services.file_crud_service import FileCRUDService

        FileCRUDService()
        assert mock_activated_repo_manager_cls.call_count == 0, (
            "BUG #1689 REGRESSION: FileCRUDService.__init__ must not "
            "construct ActivatedRepoManager eagerly. "
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


class TestOrphanedActivatedRepoManagerPropertyIsRemoved:
    """Bug #1703: the Bug #1689 lazy `activated_repo_manager` property lost
    its only production consumer when Bug #1692 redirected
    `_resolve_repo_path` onto the module-level, DI/app.state-wired
    `_get_activated_repo_manager()` function. Per Messi Rule 12
    (anti-orphan-code), the orphaned property -- and the per-instance
    lazy-construction state that existed only to support it -- was
    deleted entirely rather than merely documented as deprecated, mirroring
    the identical precedent Bug #1702 set for routers/git.py's analogous
    module-level singleton.

    These tests guard against silent reintroduction: a future developer
    reaching for `self.activated_repo_manager` on `FileCRUDService` would
    construct a fresh, NODE-LOCAL, UNPOOLED `ActivatedRepoManager` whose
    registry check can't see PostgreSQL-mode activation state -- the exact
    #1692 cluster-outage defect class.
    """

    def test_no_activated_repo_manager_property_on_class(self) -> None:
        from code_indexer.server.services.file_crud_service import FileCRUDService

        assert not hasattr(FileCRUDService, "activated_repo_manager"), (
            "BUG #1703 REGRESSION: FileCRUDService must not retain the "
            "orphaned Bug #1689 `activated_repo_manager` lazy property -- "
            "it has zero production consumers since Bug #1692 redirected "
            "_resolve_repo_path onto the module-level "
            "_get_activated_repo_manager() function. Reintroducing it "
            "re-opens the exact #1692 cluster-outage defect class for any "
            "new caller that reaches for it."
        )

    def test_no_activated_repo_manager_attribute_on_fresh_instance(self) -> None:
        from code_indexer.server.services.file_crud_service import FileCRUDService

        service = FileCRUDService()
        assert not hasattr(service, "activated_repo_manager"), (
            "BUG #1703 REGRESSION: a freshly constructed FileCRUDService "
            "must not expose an `activated_repo_manager` attribute -- the "
            "orphaned lazy property was removed entirely."
        )

    def test_no_lazy_backing_state_attributes(self) -> None:
        """The per-instance lazy-construction state
        (`_activated_repo_manager_lazy`, `_arm_initializing`) and the
        class-level `_activated_repo_manager_lock` existed only to back
        the now-removed property -- nothing else in FileCRUDService reads
        them, so they must be gone too, not left behind as dead state.
        """
        from code_indexer.server.services.file_crud_service import FileCRUDService

        service = FileCRUDService()
        assert not hasattr(FileCRUDService, "_activated_repo_manager_lock"), (
            "BUG #1703 REGRESSION: FileCRUDService must not retain the "
            "class-level lock that existed only to guard the removed "
            "lazy-construction property."
        )
        assert not hasattr(service, "_activated_repo_manager_lazy"), (
            "BUG #1703 REGRESSION: FileCRUDService.__init__ must not set "
            "the per-instance lazy-construction slot that existed only to "
            "back the removed property."
        )
        assert not hasattr(service, "_arm_initializing"), (
            "BUG #1703 REGRESSION: FileCRUDService must not retain the "
            "re-entrancy sentinel that existed only to guard the removed "
            "lazy-construction property."
        )
