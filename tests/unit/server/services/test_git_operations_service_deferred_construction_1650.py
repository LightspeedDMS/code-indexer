"""Bug #1650 remediation: GitOperationsService.__init__ itself must be cheap.

Code review of the first #1650 fix attempt (commit 2085fe9a) proved the
module-level PEP 562 lazy-init alone does NOT fix the reported symptom:
PEP 562's __getattr__ fires transparently on `from module import name` too,
and all five real consumers of `git_operations_service`
(routers/git.py, mcp/handlers/{git_read,git_write,__init__,_legacy}.py) bind
the name at MODULE SCOPE via exactly that statement. So importing any MCP
handler module still forces full `GitOperationsService()` construction via
`__getattr__` at import time -- the module-level deferral was necessary but
not sufficient.

This file tests the actual remediation ("Option A"): the two expensive
operations inside `GitOperationsService.__init__` itself --
`ActivatedRepoManager(...)` construction (-> GoldenRepoManager -> SQLite
golden-repo load, spawning bgm-worker/bgm-temporal-worker threads) and the
config-service resolution/read (`get_config_service()` /
`config_manager.get_config()`, relevant to the Bug #1428 credential-bleed
path) -- are deferred to first REAL use via properties, instead of running
unconditionally at construction time. This fixes the reported symptom
regardless of how many modules bind the `git_operations_service` name, and
needs no consumer-side changes.

Every test that constructs a real GitOperationsService keeps the
ActivatedRepoManager patch active across BOTH construction and first
access: on the pre-fix (RED) baseline, __init__ constructs
ActivatedRepoManager eagerly, so the real (unmocked) SQLite load / thread
spawn must never run regardless of which code path is under test.

Patch target note: `GitOperationsService` imports `ActivatedRepoManager`
LOCALLY (inside `__init__`/the lazy property, "to avoid circular imports" --
the original code's own comment), not as a module-level binding in
git_operations_service.py. A local `from module import Name` re-reads the
CURRENT attribute of the source module every time it executes, so the
correct interception point is
`code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager`
-- patching `code_indexer.server.services.git_operations_service.ActivatedRepoManager`
would fail outright (no such module-level attribute exists there to patch).
Verified empirically against the current production code before writing
these tests.
"""

import threading
from unittest.mock import MagicMock, patch

from code_indexer.server.services.git_operations_service import GitOperationsService

THREAD_JOIN_TIMEOUT_SECONDS = 10
BARRIER_WAIT_TIMEOUT_SECONDS = 5


class TestInitDoesNotEagerlyConstructActivatedRepoManager:
    """__init__ must not build ActivatedRepoManager (and therefore not
    GoldenRepoManager, not the SQLite golden-repo load, not the
    bgm-worker/bgm-temporal-worker threads) as a side effect of merely
    constructing GitOperationsService.
    """

    def test_construction_does_not_call_activated_repo_manager_constructor(
        self,
    ) -> None:
        fake_config_manager = MagicMock()
        fake_config_manager.get_config.return_value = None

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
        ) as mock_arm_cls:
            GitOperationsService(config_manager=fake_config_manager)
            assert mock_arm_cls.call_count == 0, (
                "BUG #1650 REMEDIATION REGRESSION: GitOperationsService.__init__ "
                "must not construct ActivatedRepoManager eagerly -- it should be "
                f"deferred to first real access. call_count={mock_arm_cls.call_count}"
            )

    def test_first_access_constructs_activated_repo_manager_exactly_once(
        self,
    ) -> None:
        fake_config_manager = MagicMock()
        fake_config_manager.get_config.return_value = None

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
        ) as mock_arm_cls:
            mock_arm_cls.return_value = "constructed-instance"
            service = GitOperationsService(config_manager=fake_config_manager)
            first = service.activated_repo_manager
            second = service.activated_repo_manager

        assert mock_arm_cls.call_count == 1, (
            "ActivatedRepoManager must be constructed exactly once, lazily, "
            f"on first real access. call_count={mock_arm_cls.call_count}"
        )
        assert first == "constructed-instance"
        assert first is second

    def test_two_threads_racing_first_access_construct_exactly_once(self) -> None:
        """Thread-safety guard: concurrent first access from two threads must
        not double-construct ActivatedRepoManager."""
        fake_config_manager = MagicMock()
        fake_config_manager.get_config.return_value = None

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
        ) as mock_arm_cls:
            mock_arm_cls.side_effect = lambda **kw: object()
            service = GitOperationsService(config_manager=fake_config_manager)

            results_lock = threading.Lock()
            results: dict = {}
            errors: dict = {}
            barrier = threading.Barrier(2)

            def worker(key: str) -> None:
                try:
                    barrier.wait(timeout=BARRIER_WAIT_TIMEOUT_SECONDS)
                    value = service.activated_repo_manager
                    with results_lock:
                        results[key] = value
                except Exception as e:  # noqa: BLE001 - captured for assertion
                    with results_lock:
                        errors[key] = e

            t1 = threading.Thread(target=worker, args=("a",))
            t2 = threading.Thread(target=worker, args=("b",))
            t1.start()
            t2.start()
            t1.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
            t2.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

        assert not t1.is_alive(), "worker thread 'a' did not finish in time"
        assert not t2.is_alive(), "worker thread 'b' did not finish in time"
        assert errors == {}, f"worker thread(s) raised an exception: {errors}"
        assert set(results) == {"a", "b"}, (
            f"expected both worker results present, got keys={set(results)}"
        )
        assert mock_arm_cls.call_count == 1, (
            "Concurrent first access must not race-construct two distinct "
            f"ActivatedRepoManager instances. call_count={mock_arm_cls.call_count}"
        )
        assert results["a"] is results["b"]


class TestActivatedRepoManagerReentrancyDoesNotRecurse:
    """Round-2 code review M2: the activated_repo_manager lazy property's
    RLock stops cross-thread deadlock but NOT same-thread re-entrant
    recursion -- on re-entry the double-checked `is None` test is still
    True (the assignment happens only after the constructor returns), so a
    re-entrant call during construction would construct AGAIN. Reviewer
    proved this reaches construction depth 6 (capped only by the probe
    itself; uncapped this is unbounded recursion -> RecursionError, and in
    production N redundant ActivatedRepoManager instances each spawning
    their own bgm-worker/bgm-temporal-worker threads).

    This exercises the fix via a background thread with a bounded
    join(timeout=...), per the CLAUDE.md "Module-Level Service Singletons
    Must Be Lazy" invariant's prescribed re-entrancy test shape -- hooking
    the (mocked) ActivatedRepoManager constructor to probe
    activated_repo_manager again mid-construction, on the same thread.
    """

    def test_reentrant_access_during_construction_does_not_recurse(self) -> None:
        fake_config_manager = MagicMock()
        fake_config_manager.get_config.return_value = None
        service = GitOperationsService(config_manager=fake_config_manager)

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
            "BUG #1650 REMEDIATION REGRESSION (M2): the constructor must run "
            "EXACTLY ONCE -- a re-entrant call arriving mid-construction "
            "must not trigger a second/recursive construction. "
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
    """Regression guard: tests/unit/server/routers/test_git_read_endpoints_contract.py
    does `git_operations_service.activated_repo_manager = mock_arm` directly on
    the real singleton -- this must keep working after the property
    conversion.
    """

    def test_direct_assignment_and_readback(self) -> None:
        fake_config_manager = MagicMock()
        fake_config_manager.get_config.return_value = None

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
        ) as mock_arm_cls:
            service = GitOperationsService(config_manager=fake_config_manager)

            sentinel = object()
            service.activated_repo_manager = sentinel
            assert service.activated_repo_manager is sentinel

            # Restoring back to lazy (None) must resume lazy construction.
            service.activated_repo_manager = None
            mock_arm_cls.return_value = "fresh-instance"
            assert service.activated_repo_manager == "fresh-instance"


class TestInitDoesNotEagerlyReadConfig:
    """__init__ must not call config_manager.get_config() eagerly -- relevant
    to the Bug #1428 credential-bleed path (get_config_service() can return
    real provider API keys)."""

    def test_construction_does_not_call_get_config(self) -> None:
        fake_config_manager = MagicMock()
        fake_config_manager.get_config.return_value = None

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
        ):
            GitOperationsService(config_manager=fake_config_manager)

        assert fake_config_manager.get_config.call_count == 0, (
            "BUG #1650 REMEDIATION REGRESSION: __init__ must not read config "
            "eagerly -- it should be deferred to first real timeout/limit "
            f"access. call_count={fake_config_manager.get_config.call_count}"
        )

    def test_first_git_timeouts_access_reads_config_exactly_once(self) -> None:
        fake_config = MagicMock()
        fake_config_manager = MagicMock()
        fake_config_manager.get_config.return_value = fake_config

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
        ):
            service = GitOperationsService(config_manager=fake_config_manager)

            # Discriminating assertion: must be genuinely zero BEFORE first
            # access, not just "one" after -- the pre-fix eager
            # implementation also produces exactly one call by this point
            # (during __init__), so asserting only the post-access total
            # would pass even on the bug.
            assert fake_config_manager.get_config.call_count == 0, (
                "get_config() must not have run yet -- construction alone "
                "must not trigger a config read."
            )

            _ = service._git_timeouts
            _ = service._git_timeouts
            _ = service._api_limits

        assert fake_config_manager.get_config.call_count == 1, (
            "Config must be read exactly once, lazily, on first real "
            f"timeout/limit access. call_count={fake_config_manager.get_config.call_count}"
        )
        assert service._git_timeouts is fake_config.git_timeouts_config
        assert service._api_limits is fake_config.api_limits_config

    def test_explicitly_set_git_timeouts_survives_later_api_limits_read(
        self,
    ) -> None:
        """Round-2 code review M1: _ensure_config_loaded() must not silently
        clobber an already-set _git_timeouts_lazy/_api_limits_lazy slot.

        Reproduces the reviewer's exact probe: set _git_timeouts to a
        sentinel, then read _api_limits (which triggers
        _ensure_config_loaded() since _api_limits_lazy is still None) --
        the sentinel must survive, not be silently overwritten by the
        config-derived value. Verified independently before this fix: the
        sentinel was replaced by config.git_timeouts_config.
        """
        fake_config = MagicMock()
        fake_config_manager = MagicMock()
        fake_config_manager.get_config.return_value = fake_config

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
        ):
            service = GitOperationsService(config_manager=fake_config_manager)

        sentinel = object()
        service._git_timeouts = sentinel

        _ = service._api_limits

        assert service._git_timeouts is sentinel, (
            "BUG #1650 REMEDIATION REGRESSION (M1): explicitly setting "
            "_git_timeouts must survive a later _api_limits read -- "
            "_ensure_config_loaded() must not unconditionally overwrite an "
            f"already-set slot. Got: {service._git_timeouts!r}"
        )
        assert service._api_limits is fake_config.api_limits_config

    def test_no_config_manager_arg_defers_get_config_service_call(self) -> None:
        """When no config_manager is passed, the module-level
        get_config_service() singleton getter must not be invoked during
        __init__ -- only on first real config read."""
        with (
            patch(
                "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
            ),
            patch(
                "code_indexer.server.services.config_service.get_config_service"
            ) as mock_get_config_service,
        ):
            fake_config_manager = MagicMock()
            fake_config_manager.get_config.return_value = None
            mock_get_config_service.return_value = fake_config_manager

            service = GitOperationsService()  # no config_manager argument

            assert mock_get_config_service.call_count == 0, (
                "BUG #1650 REMEDIATION REGRESSION: get_config_service() must not "
                "be called during __init__ when no config_manager argument is "
                "supplied -- it must be deferred to first real config read. "
                f"call_count={mock_get_config_service.call_count}"
            )

            _ = service._git_timeouts

            assert mock_get_config_service.call_count == 1
            assert fake_config_manager.get_config.call_count == 1


class TestGitTimeoutsSetterStillWorksForBareNewBypass:
    """Regression guard: 5 existing test files construct GitOperationsService
    via `GitOperationsService.__new__(GitOperationsService)` (bypassing
    __init__ entirely) then do `service._git_timeouts = mock_timeouts`
    directly. This must keep working after the property conversion, without
    ever touching config_manager/get_config (which would fail since __init__
    never ran).
    """

    def test_new_bypass_then_direct_timeouts_assignment(self) -> None:
        service = GitOperationsService.__new__(GitOperationsService)
        timeouts = MagicMock()
        timeouts.git_local_timeout = 30
        service._git_timeouts = timeouts

        assert service._git_timeouts is timeouts
        assert service._git_timeouts.git_local_timeout == 30
