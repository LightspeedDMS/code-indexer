"""Bug #1638 code-review repair: regression tests for the sentinel-based
lazy-init redesign of server/app.py's module-level __getattr__.

The original fix (commit ebdcf492) replaced app.py's unconditional
`app = create_app()` import-time side effect with a PEP 562 __getattr__ that
lazily calls create_app() on first genuine attribute access. That first
attempt used a plain `threading.Lock()` acquired for the duration of
create_app(), which was REJECTED by code review with 4 blocking findings,
all reproduced independently:

  1. Re-entrant self-deadlock: create_app()'s own execution path
     (initialize_services() -> bootstrap_cidx_meta() ->
     golden_repo_manager.register_local_repo() ->
     global_activator.activate_golden_repo() -> registry property ->
     server/utils/registry_factory.py's resolve_backend_registry_state() ->
     resolve_backend_registry_attr() -> _running_server_app_state()) calls
     getattr(app_module, "app", None) on THIS SAME MODULE, re-entering
     __getattr__ on the SAME thread while the non-reentrant Lock is still
     held -- deadlocking forever. Only reproduces on a genuinely fresh node
     (cidx-meta not yet bootstrapped), which is why every dev/test
     environment (cidx-meta already bootstrapped) masked it.
  2. unittest.mock.patch teardown KeyError: patching a lazy name that was
     never yet materialized in globals() causes mock's __exit__ to call
     delattr() then hasattr() (re-entering __getattr__ with the name freshly
     deleted), and the old code's bare `return globals()[name]` raised
     KeyError -- which hasattr() does not catch -- permanently poisoning the
     module for every later read of that name.
  3. langfuse_sync_service is listed in _LAZY_INIT_ATTRS but create_app()
     never assigns it via a `global` statement, so `globals()[name]` raised
     KeyError instead of resolving to None (its pre-fix default), breaking
     the getattr(module, name, default)/hasattr() protocol.
  4. mypy regression: __getattr__(name: str) -> Any erased the concrete
     FastAPI type at 5 call sites in search_service.py. Fixed separately in
     that file via TYPE_CHECKING + cast("FastAPI", ...); not covered here.

The remediation replaces the plain Lock with an RLock guarding an explicit
_initializing/_initialized sentinel pair plus a _lazy_values snapshot -- see
the module-level comments in src/code_indexer/server/app.py above
_lazy_init_lock and inside _ensure_initialized()/__getattr__ for the full
design rationale.
"""

import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent / "src")
SUBPROCESS_TIMEOUT_SECONDS = 30


class TestReentrantProbeDuringCreateAppDoesNotDeadlock:
    """Blocker #1: a re-entrant getattr(app_module, "app", None) arriving
    from inside create_app()'s own execution must return None immediately
    -- never hang, never recurse into a second create_app() call.

    This test uses a FRESH SUBPROCESS (mirroring the established pattern in
    test_app_import_no_side_effects_1638.py) so the module starts pristine.

    Design note (stated per the reviewer's explicit instruction to declare
    which approach was used and why): this exercises the REAL, unmodified
    production code for everything under test -- app.py's real
    __getattr__/_ensure_initialized() lock+sentinel logic, the real
    create_app(), and the real _running_server_app_state() from
    code_indexer.server.utils.registry_factory (the exact function named in
    the bug report's reproduction chain) as the re-entrant probe. Only
    `initialize_services()` -- a distinct module (startup/service_init.py)
    that does real DB/filesystem/subprocess bootstrap work -- is replaced
    with a stand-in that performs the SAME re-entrant probe create_app()'s
    real call chain performs before signaling completion. Fully
    bootstrapping cidx-meta from a bare subprocess (spawning a real `cidx
    init` subprocess, real SQLite writes) to hit this exact line is
    impractical for a fast, deterministic unit test and would not exercise
    any additional part of the lock/sentinel mechanism actually being
    fixed here -- initialize_services() is a plain external dependency
    boundary of create_app(), not the code under test.
    """

    def test_reentrant_probe_returns_none_with_exactly_one_init_call(
        self,
    ) -> None:
        script = f"""
import sys
sys.path.insert(0, {SRC_ROOT!r})
import threading

import code_indexer.server.app as app_module
import code_indexer.server.startup.service_init as service_init_module
from code_indexer.server.utils.registry_factory import _running_server_app_state

call_count = {{"n": 0}}
captured = {{}}


def fake_initialize_services():
    call_count["n"] += 1
    # Real production re-entrant probe from the bug report's exact chain:
    # bootstrap_cidx_meta() -> ... -> GlobalActivator.registry ->
    # resolve_backend_registry_state() -> resolve_backend_registry_attr() ->
    # _running_server_app_state() -> getattr(app_module, "app", None).
    # This fires from INSIDE create_app()'s real, unmodified execution.
    captured["reentrant"] = _running_server_app_state()
    raise RuntimeError("stubbed-initialize-services-stop-here")


service_init_module.initialize_services = fake_initialize_services

result = {{}}


def worker():
    try:
        result["app"] = app_module.app  # real __getattr__ -> _ensure_initialized -> real create_app()
    except Exception as e:
        result["exception"] = repr(e)


t = threading.Thread(target=worker, daemon=True)
t.start()
t.join(timeout=15)

print("thread_alive:", t.is_alive())
print("call_count:", call_count["n"])
print("reentrant_is_none:", captured.get("reentrant") is None)
print("exception_seen:", "exception" in result)
print("exception_is_stub_marker:", "stubbed-initialize-services-stop-here" in result.get("exception", ""))
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
            "BLOCKER #1 REGRESSION: __getattr__ hung (deadlocked) while a "
            f"re-entrant probe fired during create_app()'s own execution. "
            f"Subprocess output: {stdout!r}"
        )
        assert "call_count: 1" in stdout, (
            "initialize_services() must run EXACTLY ONCE -- a re-entrant "
            f"probe must not trigger a second/recursive initialization. "
            f"Output: {stdout!r}"
        )
        assert "reentrant_is_none: True" in stdout, (
            "The re-entrant getattr(app_module, 'app', None) must resolve to "
            "None (matching pre-fix semantics: `app` was genuinely unbound "
            f"until the module-level assignment completed). Output: {stdout!r}"
        )
        assert "exception_seen: True" in stdout, (
            f"The ORIGINAL (outer) call must observe the stub's exception "
            f"once create_app() finishes unwinding. Output: {stdout!r}"
        )
        assert "exception_is_stub_marker: True" in stdout, stdout


class TestMockPatchRoundTrip:
    """Blocker #2: patching a lazy attribute that is absent from the
    module's __dict__ at patch time must restore cleanly on teardown --
    no KeyError, and the original value must be genuinely recoverable
    afterward via the _lazy_values snapshot fallback.
    """

    def test_mock_patch_of_lazy_attr_absent_from_dict_restores_cleanly(
        self,
    ) -> None:
        import code_indexer.server.app as app_module

        name = "golden_repo_manager"

        # Bug #1657: force _ensure_initialized() to have run at least once
        # in THIS process (via a genuine lazy attribute access, `app`)
        # BEFORE reading `original` below. This guarantees _initialized is
        # already True by the time the mock.patch round trip executes, so
        # its internal getattr()/hasattr() fallback calls are guaranteed
        # no-ops with respect to _ensure_initialized() and can never
        # silently re-run create_app() (which would overwrite the
        # _lazy_values resync performed a few lines down with yet another,
        # unrelated GoldenRepoManager instance -- observed directly while
        # diagnosing this bug: _initialized was still False at test start
        # in a warm process where only OTHER tests had called create_app()
        # directly without ever touching app_module.app, so mock.patch's
        # own fallback getattr() triggered the first-ever
        # _ensure_initialized(), clobbering the resync below).
        _ = app_module.app

        # Force real initialization (if not already done in this process)
        # so we know the genuine baseline value to compare against later.
        original = getattr(app_module, name)
        assert original is not None

        # Dozens of OTHER server test files legitimately call create_app()
        # directly (e.g. test_auth_endpoints.py building a fresh per-test
        # FastAPI app) to get their own isolated app instance. Each such
        # call reassigns app_module's globals() for every _LAZY_INIT_ATTRS
        # name via `global golden_repo_manager; ...` WITHOUT going through
        # _ensure_initialized() -- so it never refreshes _lazy_values,
        # which (now that _initialized is guaranteed True above) is
        # snapshotted exactly ONCE, at the very first _ensure_initialized()
        # call for the whole worker process's lifetime. In a warm, shared
        # pytest process this lets globals()[name] (what `original` above
        # just read) silently diverge from _lazy_values[name] (the
        # mock.patch fallback this test exercises) depending purely on
        # which other tests happened to run earlier in the SAME process --
        # reproduced directly by running this test alongside
        # test_auth_endpoints.py, which failed with two distinct
        # GoldenRepoManager instances. That divergence is a same-process
        # test-ordering artifact, not a production bug (production calls
        # create_app() exactly once) and not genuine pytest-xdist
        # concurrency (workers are separate processes and share no
        # memory). Resync the snapshot to what this test just observed so
        # its own assertion is discriminating regardless of execution
        # order, restoring the prior snapshot value afterward.
        had_lazy_value = name in app_module._lazy_values
        saved_lazy_value = app_module._lazy_values.get(name)
        app_module._lazy_values[name] = original

        # Reproduce the exact Blocker #2 precondition: the name is absent
        # from __dict__ at patch time (e.g. because create_app() ran via
        # _ensure_initialized() but nothing has re-materialized it into
        # __dict__ since, or because an earlier mock.patch in a warm suite
        # already delattr'd it) -- regardless of whatever order other tests
        # in a full suite run may have left this shared module singleton in.
        had_local = name in app_module.__dict__
        saved_local = app_module.__dict__.get(name)
        app_module.__dict__.pop(name, None)

        try:
            assert name not in app_module.__dict__

            with mock.patch(f"code_indexer.server.app.{name}", "mocked-sentinel"):
                assert app_module.golden_repo_manager == "mocked-sentinel"

            # The mock.patch __exit__ must NOT raise KeyError here (Blocker #2).
            restored = app_module.golden_repo_manager
            assert restored is original, (
                "BLOCKER #2 REGRESSION: mock.patch teardown must restore the "
                "original value via the _lazy_values snapshot fallback when "
                "the name is absent from __dict__, not raise KeyError or "
                "leave the attribute permanently unreadable."
            )
        finally:
            if had_local:
                app_module.__dict__[name] = saved_local
            else:
                app_module.__dict__.pop(name, None)
            if had_lazy_value:
                app_module._lazy_values[name] = saved_lazy_value
            else:
                app_module._lazy_values.pop(name, None)

    def test_mock_patch_teardown_raises_no_exception(self) -> None:
        """Explicit no-exception assertion for the exact teardown sequence
        that raised KeyError pre-repair (delattr then hasattr probe).
        """
        import code_indexer.server.app as app_module

        name = "activated_repo_manager"
        _ = getattr(app_module, name)  # ensure initialized

        had_local = name in app_module.__dict__
        saved_local = app_module.__dict__.get(name)
        app_module.__dict__.pop(name, None)

        try:
            patcher = mock.patch(f"code_indexer.server.app.{name}", "temp-value")
            patcher.start()
            assert app_module.activated_repo_manager == "temp-value"
            # patcher.stop() is exactly where the old code raised KeyError.
            patcher.stop()
        finally:
            if had_local:
                app_module.__dict__[name] = saved_local
            else:
                app_module.__dict__.pop(name, None)


class TestLangfuseSyncServiceResolvesToNone:
    """Blocker #3: langfuse_sync_service is never assigned by create_app()
    via a `global` statement, so it must resolve to None (its pre-fix
    default) -- never KeyError -- both via direct access and via the
    getattr(module, name, default) protocol.
    """

    def test_direct_attribute_access_resolves_to_none(self) -> None:
        import code_indexer.server.app as app_module

        # Ensure initialized so _lazy_values is populated.
        _ = app_module.app

        assert app_module.langfuse_sync_service is None, (
            "BLOCKER #3 REGRESSION: langfuse_sync_service must resolve to "
            "None via the _lazy_values snapshot fallback (create_app() never "
            "assigns it via `global`), not raise KeyError."
        )

    def test_getattr_with_default_resolves_to_none_not_the_default(self) -> None:
        """The design's _lazy_values snapshot captures langfuse_sync_service
        as None (a REAL resolved value, not "truly absent"), so
        getattr(module, name, default) must return None -- honoring the
        protocol correctly for a name that genuinely resolves, as opposed to
        falling through to the caller-supplied default.
        """
        import code_indexer.server.app as app_module

        _ = app_module.app  # ensure initialized

        sentinel_default = "sentinel_default_never_expected"
        result = getattr(app_module, "langfuse_sync_service", sentinel_default)
        assert result is None
        assert result != sentinel_default


class TestNonLazyNonexistentAttributeUnaffected:
    """Item #4: a genuinely nonexistent, non-lazy name must still raise
    AttributeError / honor getattr's default via normal Python semantics --
    unaffected by the sentinel redesign.
    """

    def test_bogus_attribute_raises_attribute_error(self) -> None:
        import code_indexer.server.app as app_module

        with pytest.raises(AttributeError):
            _ = app_module.totally_bogus_attribute_name_xyz_1638  # type: ignore[attr-defined]

    def test_bogus_attribute_getattr_returns_default(self) -> None:
        import code_indexer.server.app as app_module

        sentinel = object()
        result = getattr(app_module, "totally_bogus_attribute_name_xyz_1638", sentinel)
        assert result is sentinel
