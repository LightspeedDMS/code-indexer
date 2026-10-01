"""Bug #1650: importing git_operations_service.py must not run full service init.

Prior to the fix, `server/services/git_operations_service.py` had a
module-level statement `git_operations_service = _get_git_operations_service()`
that ran unconditionally at import time. Any code that merely imports
`code_indexer.server.services.git_operations_service` -- or a module that
transitively imports it without ever touching the singleton -- triggered a
real `GitOperationsService()` -> `ActivatedRepoManager()` ->
`GoldenRepoManager()` -> `_load_metadata_from_sqlite()` construction chain,
with no explicit opt-in. This is the same class of anti-pattern bug #1638
fixed in `server/app.py`.

These tests use a fresh subprocess (mirroring the established pattern in
tests/unit/server/test_app_import_no_side_effects_1638.py) so the Python
interpreter is clean -- no contamination from other test files having
already imported the module in-process.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent.parent / "src")
SUBPROCESS_TIMEOUT_SECONDS = 60
REENTRANT_PROBE_JOIN_TIMEOUT_SECONDS = 15


def _run_and_assert_ok(code: str) -> str:
    """Run `code` in a fresh subprocess, assert clean exit, return stdout."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout


class TestBareImportDoesNotConstructGitOperationsService:
    """A bare import of git_operations_service.py must be inert."""

    def test_direct_module_import_does_not_construct_service(self) -> None:
        """`import code_indexer.server.services.git_operations_service` must
        not construct the GitOperationsService singleton as an import-time
        side effect.
        """
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.services.git_operations_service as gos_module; "
            "print('service_in_dict:', 'git_operations_service' in vars(gos_module))"
        )
        stdout = _run_and_assert_ok(code)
        assert "service_in_dict: False" in stdout, (
            "BUG #1650: importing git_operations_service.py as a bare module "
            "(without touching the `git_operations_service` attribute) "
            "constructed the GitOperationsService singleton eagerly "
            f"(GitOperationsService -> ActivatedRepoManager -> "
            f"GoldenRepoManager -> SQLite load). Subprocess output: {stdout!r}"
        )

    def test_bare_import_does_not_construct_activated_repo_manager(self) -> None:
        """Empirically-observed symptom: a bare import must not build a real
        ActivatedRepoManager (which itself builds a GoldenRepoManager and
        loads golden repos from SQLite, and spawns bgm-worker threads).
        """
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import threading; "
            "before = {t.name for t in threading.enumerate()}; "
            "import code_indexer.server.services.git_operations_service as gos_module; "
            "after = {t.name for t in threading.enumerate()}; "
            "new_threads = after - before; "
            "bgm_threads = [n for n in new_threads if 'bgm' in n.lower()]; "
            "print('new_bgm_threads:', bgm_threads); "
            "print('service_in_dict:', 'git_operations_service' in vars(gos_module))"
        )
        stdout = _run_and_assert_ok(code)
        assert "new_bgm_threads: []" in stdout, (
            "BUG #1650: a bare import spawned background worker threads as "
            f"an import-time side effect. Subprocess output: {stdout!r}"
        )
        assert "service_in_dict: False" in stdout


class TestExplicitAccessStillInitializesCorrectly:
    """Real usage -- explicit access to `git_operations_service` -- must
    still fully and correctly construct the service (regression guard for
    the production REST/MCP handler wiring)."""

    def test_explicit_attribute_access_constructs_real_service(self) -> None:
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.services.git_operations_service as gos_module; "
            "svc = gos_module.git_operations_service; "
            "from code_indexer.server.services.git_operations_service import GitOperationsService; "
            "print('is_service:', isinstance(svc, GitOperationsService)); "
            "print('has_activated_repo_manager:', "
            "getattr(svc, 'activated_repo_manager', None) is not None)"
        )
        stdout = _run_and_assert_ok(code)
        assert "is_service: True" in stdout
        assert "has_activated_repo_manager: True" in stdout

    def test_from_import_of_singleton_still_lazily_initializes(self) -> None:
        """`from code_indexer.server.services.git_operations_service import
        git_operations_service` (the pattern used by routers/git.py and the
        MCP git handlers) must still resolve to a real, fully-initialized
        service instance -- it is an explicit reference, not a bare import.
        """
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.server.services.git_operations_service import git_operations_service; "
            "print('service_is_none:', git_operations_service is None)"
        )
        stdout = _run_and_assert_ok(code)
        assert "service_is_none: False" in stdout

    def test_repeated_access_returns_same_singleton_instance(self) -> None:
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import code_indexer.server.services.git_operations_service as gos_module; "
            "a = gos_module.git_operations_service; "
            "b = gos_module.git_operations_service; "
            "print('same_instance:', a is b)"
        )
        stdout = _run_and_assert_ok(code)
        assert "same_instance: True" in stdout


class TestReentrantAccessDuringConstructionDoesNotDeadlock:
    """Discriminating test for the #1638 first-round bug class: a re-entrant
    access to the lazy attribute arriving from WITHIN the initialization
    call chain (same thread) must not deadlock. This requires an RLock, not
    a plain Lock -- a plain Lock self-deadlocks the moment the lazily
    invoked constructor's own call chain reads the module attribute again
    before the first call has finished.

    Design note (mirrors test_app_lazy_init_repair_1638.py's Blocker #1
    rationale): this exercises the REAL, unmodified production
    __getattr__ / _ensure_initialized() lock+sentinel logic and the REAL
    GitOperationsService()/_get_git_operations_service() production code.
    Only `TTLCache` -- a third-party EXTERNAL dependency (cachetools),
    still constructed unconditionally inside GitOperationsService.__init__
    even after the Bug #1650 Option A remediation moved
    ActivatedRepoManager/config-service resolution off the __init__ call
    chain entirely -- is replaced with a stand-in that performs a
    re-entrant probe of the module's own lazy attribute before returning,
    simulating a future/indirect call chain re-entering __getattr__ on the
    same thread (exactly like #1638's _running_server_app_state() probe did
    for app.py's create_app() -> ... -> registry chain). Verified
    empirically that patching TTLCache here intercepts exactly the one call
    __init__ makes, with no double-invocation.
    """

    def test_reentrant_probe_during_construction_returns_none_no_deadlock(
        self,
    ) -> None:
        script = f"""
import sys
sys.path.insert(0, {SRC_ROOT!r})
import threading
from unittest import mock

import code_indexer.server.services.git_operations_service as gos_module

call_count = {{"n": 0}}
captured = {{}}


class ProbingTTLCache:
    def __init__(self, *args, **kwargs):
        call_count["n"] += 1
        # Re-entrant probe from WITHIN the lazy-construction call chain, on
        # the SAME thread -- exactly the shape of #1638's Blocker #1 (a
        # re-entrant getattr() firing before the original call finishes).
        captured["reentrant"] = getattr(gos_module, "git_operations_service", None)


result = {{}}


def worker():
    try:
        with mock.patch.object(gos_module, "TTLCache", ProbingTTLCache):
            result["service"] = gos_module.git_operations_service
    except Exception as e:
        result["exception"] = repr(e)


t = threading.Thread(target=worker, daemon=True)
t.start()
t.join(timeout={REENTRANT_PROBE_JOIN_TIMEOUT_SECONDS})

print("thread_alive:", t.is_alive())
print("call_count:", call_count["n"])
print("reentrant_is_none:", captured.get("reentrant") is None)
print("service_constructed:", result.get("service") is not None)
print("exception_seen:", "exception" in result)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        assert result.returncode == 0, (
            f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        stdout = result.stdout
        assert "thread_alive: False" in stdout, (
            "REGRESSION: __getattr__ hung (deadlocked) while a re-entrant "
            f"probe fired during construction. Subprocess output: {stdout!r}"
        )
        assert "call_count: 1" in stdout, (
            "The constructor must run EXACTLY ONCE -- a re-entrant probe "
            f"must not trigger a second/recursive construction. Output: {stdout!r}"
        )
        assert "reentrant_is_none: True" in stdout, (
            "The re-entrant getattr(module, 'git_operations_service', None) "
            "must resolve to None while construction is still in flight "
            f"(matching pre-fix unbound semantics). Output: {stdout!r}"
        )
        assert "service_constructed: True" in stdout, stdout
        assert "exception_seen: False" in stdout, (
            f"The outer (original) call must complete successfully once "
            f"construction finishes unwinding. Output: {stdout!r}"
        )


class TestNonLazyNonexistentAttributeUnaffected:
    """A genuinely nonexistent, non-lazy name must still raise
    AttributeError / honor getattr's default via normal Python semantics."""

    def test_bogus_attribute_raises_attribute_error(self) -> None:
        import code_indexer.server.services.git_operations_service as gos_module

        with pytest.raises(AttributeError):
            _ = gos_module.totally_bogus_attribute_name_xyz_1650  # type: ignore[attr-defined]

    def test_bogus_attribute_getattr_returns_default(self) -> None:
        import code_indexer.server.services.git_operations_service as gos_module

        sentinel = object()
        result = getattr(gos_module, "totally_bogus_attribute_name_xyz_1650", sentinel)
        assert result is sentinel
