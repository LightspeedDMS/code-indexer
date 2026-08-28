"""Shared pytest fixtures for tests/unit/server/.

Bug #1635: BackgroundJobManager.__init__ unconditionally starts a fixed pool
of long-lived worker threads at construction time (5 ordinary `bgm-worker-*`
+ 2 `bgm-temporal-worker-*`, all daemon=True but never exiting until
.shutdown() sets an internal Event). Tests that construct BackgroundJobManager
directly and never call .shutdown() leave those threads alive for the rest of
the pytest process's lifetime. Across the full tests/unit/server/ suite
(~14,500 tests in one process) this accumulates without bound -- confirmed
root cause of a real production incident where a full-suite run reached
5,768 live threads and drove the host's load average to 1449, requiring an
emergency SIGKILL.

`background_job_manager_factory` is the fix for the simplest case: a factory
fixture that constructs BackgroundJobManager with whatever args/kwargs the
caller passes (identical signature to the real constructor), tracks every
instance it creates, and guarantees `.shutdown()` is attempted for each of
them during fixture teardown -- even if the test raises, and even if one
instance's shutdown() itself raises (each instance is torn down
independently so one failure never skips the rest).

Usage:
    def test_something(background_job_manager_factory):
        bjm = background_job_manager_factory(storage_path=str(tmp_path / "jobs.json"))
        ...

Prefer this factory over constructing BackgroundJobManager(...) directly in
new tests. It remains fully supported and coexists safely with the
universal mechanism below (BackgroundJobManager.shutdown() is idempotent,
so an instance torn down by both mechanisms is torn down safely twice).

---

## Universal teardown (3rd review remediation of Bug #1635)

Two earlier remediation passes closed leak vectors one call site at a time:

1. `background_job_manager_factory` (above) covers tests that construct
   BackgroundJobManager directly.
2. A since-REMOVED mechanism monkeypatched `create_fastapi_app` in
   `code_indexer.server.startup.app_wiring` (and its `src.`-prefixed
   duplicate module -- see below) so `create_app()`'s internally-built
   BackgroundJobManager (`app.state.background_job_manager`) could also be
   torn down. It had two proven defects: the wrapper it substituted for the
   real `create_fastapi_app` did not preserve `__wrapped__`/introspection
   metadata, breaking `inspect.getsource(app_wiring.create_fastapi_app)` in
   `tests/unit/server/startup/test_app_wiring_consumer_rate_limiter_pool_1332.py`;
   and its `sys.modules.get(...)` alias detection never FORCED the
   `src.`-prefixed alias module to be imported, so test files that import
   `from src.code_indexer.server.app import create_app` only inside test
   bodies (not at module level) could run before any other file had
   incidentally imported the alias, silently bypassing the patch.

   Both defects, plus a third leak vector that mechanism could never reach
   at all -- `ActivatedRepoManager`, `SemanticQueryManager`, and
   `ActivatedRepoIndexManager` each fall back to constructing their OWN
   `BackgroundJobManager()` internally
   (`self.background_job_manager = background_job_manager or
   BackgroundJobManager()`) whenever no explicit `background_job_manager=`
   is injected, with no dependency on `create_fastapi_app` or `app.state`
   whatsoever -- are why that mechanism was removed and replaced by the
   single mechanism below.

The universal mechanism monkeypatches `BackgroundJobManager.__init__`
itself, at the CLASS level, for the duration of every test. Since every one
of the leak vectors above -- direct construction, `create_app()`'s internal
construction, and the three implicit-default production classes --
ultimately calls the SAME class's constructor, patching `__init__` once
intercepts construction regardless of which module, alias, or caller
invoked it. This closes all three vectors (plus any future one with the
same shape) with one piece of bookkeeping instead of one per call site.

The one wrinkle: `code_indexer.server.repositories.background_jobs` and its
`src.`-prefixed duplicate (`from src.code_indexer.server.app import
create_app` resolves an entirely separate module tree -- confirmed via
`id()`: same on-disk file, two distinct module objects, two distinct
`BackgroundJobManager` class objects) are genuinely different Python
classes at runtime, because every production call site in this codebase
reaches `BackgroundJobManager` via a *relative* import
(`from .background_jobs import BackgroundJobManager` /
`from ..repositories.background_jobs import BackgroundJobManager`), which
resolves within whichever top-level package root (`code_indexer` or
`src.code_indexer`) the importing module itself was loaded under.
Patching only the canonical class would silently miss every construction
that happens via the `src.`-prefixed alias tree. Unlike the old
`create_fastapi_app` mechanism, this alias is resolved via an
UNCONDITIONAL, forced `importlib.import_module(...)` (never
`sys.modules.get(...)`), closing the exact ordering bug described above --
the small one-time cost of importing the (lightweight, dependency-light)
`background_jobs` module under its alias name is worth never depending on
some other test file's import order.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Generator, List, Set, Type

import pytest

import code_indexer.server.app as _server_app_module
import code_indexer.server.auth.dependencies as _auth_dependencies_module
from code_indexer.server.auth.login_rate_limiter import (
    login_rate_limiter as _login_lockout_limiter,
)
from code_indexer.server.auth.token_bucket import rate_limiter as _login_token_bucket
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.telemetry.correlation_bridge import _correlation_id_var

logger = logging.getLogger(__name__)


def _reset_login_rate_limiter_state() -> None:
    """Clear the process-wide login rate-limiter singletons to a clean
    state (#1707).

    `token_bucket.rate_limiter` (TokenBucketManager, Story #555) and
    `login_rate_limiter.login_rate_limiter` (LoginRateLimiter, Story #557)
    are module-level singletons shared by every real `/auth/login` call
    across the WHOLE pytest process, regardless of which `create_app()`
    instance handles the request. Neither had a reset wired into this
    tree-wide conftest.py: the auth/ subdirectory's own `reset_singletons`
    fixture (tests/unit/server/auth/conftest.py, #1698) does not cover
    either of these two singletons, and does not apply outside that one
    subdirectory anyway.

    Confirmed live (2026-08-27):
    `test_health_check_endpoint.py`'s `admin_token` fixture deliberately
    logs in with a WRONG password once per test (14 real failed attempts
    against username "admin" in that one file alone). LoginRateLimiter
    locks an account out for 15 minutes after 5 failures in its sliding
    window, so every LATER test in the same pytest process that performs a
    real login as "admin" -- `test_login_validation.py`,
    `test_placeholder_endpoints_501.py`,
    `test_jwt_restart_persistence_e2e.py` -- got HTTP 429 instead of a
    real auth outcome. Resetting both singletons' internal dicts directly
    (there is no public reset() method on either class) mirrors the
    established #1698 TokenBlacklist reset pattern.

    Both singletons guard their internal dicts with their own
    `threading.Lock` for real request-time consume()/refund()/is_locked()
    access -- clearing must acquire the SAME lock objects to avoid racing
    a concurrent in-flight login request in this process (e.g. a
    background thread from a prior test's not-yet-finished server).

    Also resets `_pool` to None on both singletons (#1707 review
    remediation, Finding 4), mirroring the established #1698
    TokenBlacklist reset pattern: `app_wiring.py` calls
    `.set_connection_pool(...)` on these same singletons whenever a
    PostgreSQL backend is configured, and without this reset a future
    postgres-mode app-construction test would permanently switch every
    later test in the process onto a stale PG pool.
    """
    with _login_token_bucket._lock:
        _login_token_bucket._buckets.clear()
        _login_token_bucket._last_access.clear()
        _login_token_bucket._pool = None
    with _login_lockout_limiter._lock:
        _login_lockout_limiter._failures.clear()
        _login_lockout_limiter._lockout_until.clear()
        _login_lockout_limiter._pool = None


@pytest.fixture(autouse=True)
def _reset_login_rate_limiters() -> Generator[None, None, None]:
    """Bug #1707: tree-wide autouse fixture that resets the login
    token-bucket and account-lockout singletons before and after every
    test under tests/unit/server/. See `_reset_login_rate_limiter_state`
    above for the full rationale and the measured leak evidence this
    replaces.
    """
    _reset_login_rate_limiter_state()
    try:
        yield
    finally:
        _reset_login_rate_limiter_state()


# Bug #1727: the auth-dependency block `create_fastapi_app()` rebinds
# atomically (app_wiring.py:220-227) with no save/restore. Absolute import
# there means only this canonical module ever needs handling (no
# `src.`-prefixed alias, unlike BackgroundJobManager above).
_AUTH_DEPENDENCIES_ATTRS = (
    "jwt_manager",
    "user_manager",
    "oauth_manager",
    "mcp_credential_manager",
    "api_key_manager",
)


def _snapshot_restore_auth_dependencies_impl() -> Generator[None, None, None]:
    """Core generator body for `_snapshot_restore_auth_dependencies` below;
    extracted so a unit test can drive it via `next()` without pytest's
    fixture machinery (mirrors `_snapshot_restore_shared_app_state_impl`).
    """
    snapshot = {
        attr: getattr(_auth_dependencies_module, attr)
        for attr in _AUTH_DEPENDENCIES_ATTRS
    }
    try:
        yield
    finally:
        for attr, value in snapshot.items():
            setattr(_auth_dependencies_module, attr, value)


@pytest.fixture(autouse=True)
def _snapshot_restore_auth_dependencies() -> Generator[None, None, None]:
    """Bug #1727: snapshot and restore the auth-dependencies module-global
    block around every test under tests/unit/server/.
    """
    yield from _snapshot_restore_auth_dependencies_impl()


@pytest.fixture(autouse=True)
def _reset_correlation_id_contextvar() -> Generator[None, None, None]:
    """Bug #1648 (code-review round 2, Finding 1): force-clear the shared
    correlation-id ContextVar (`telemetry.correlation_bridge._correlation_id_var`)
    before and after every test under tests/unit/server/.

    Before GlobalErrorHandler._resolve_correlation_id() started reading this
    ContextVar back (Bug #1648's fix), a test calling
    set_current_correlation_id(...) without a matching clear was harmless --
    nothing consumed the leaked value. Now it silently poisons every later
    test in the same pytest process: proven reproducible by combining
    tests/unit/server/telemetry/test_custom_spans.py or
    tests/unit/server/services/test_audit_flush_race_1295.py (both leak an
    uncleared correlation id) with
    tests/unit/server/middleware/test_unhandled_exception_handling.py in a
    single pytest invocation.

    This single, tree-wide fixture replaces per-file fixtures (which
    duplicated the same reasoning file-by-file and, in two cases, disagreed
    on teardown mechanism -- `.set(None)` vs `.reset(token)`). `.set(None)`
    is used deliberately in both places here rather than `.reset(token)`:
    reset() would merely restore whatever -- possibly already dirty --
    value preceded this fixture's own setup, propagating rather than fixing
    any pre-existing leak from a test file that does not yet use this
    fixture (e.g. one collected before this conftest.py existed, or a
    module-scoped test running outside tests/unit/server/).
    """
    _correlation_id_var.set(None)
    try:
        yield
    finally:
        _correlation_id_var.set(None)


def _safe_shutdown(manager: Any) -> None:
    """Best-effort `.shutdown()` for one BackgroundJobManager instance.

    A failure here must never prevent tearing down the remaining tracked
    instances. `BackgroundJobManager.shutdown()` is idempotent by
    construction (`self._pool_shutdown` is a `threading.Event` -- `.set()`
    is safe to call more than once -- and thread joins are guarded by
    `thread.is_alive()`, so a second call on an already-shut-down instance
    is a fast no-op), which is what makes it safe for this helper to be
    invoked more than once on the same instance -- e.g. once by a test's
    own explicit `.shutdown()` call, and again by one (or both) of the
    fixtures below.
    """
    try:
        manager.shutdown()
    except Exception:
        logger.exception(
            "BackgroundJobManager teardown: shutdown() failed for one "
            "instance; continuing to tear down the remaining tracked "
            "instances."
        )


@pytest.fixture
def background_job_manager_factory() -> Generator[
    Callable[..., BackgroundJobManager], None, None
]:
    """Factory fixture: call like the BackgroundJobManager constructor.

    Every instance created through the returned factory is shut down
    automatically during teardown, regardless of test outcome. A shutdown
    failure on one instance is logged and does not prevent the remaining
    instances from being torn down.
    """
    instances: List[BackgroundJobManager] = []

    def _create(*args: Any, **kwargs: Any) -> BackgroundJobManager:
        manager = BackgroundJobManager(*args, **kwargs)
        instances.append(manager)
        return manager

    yield _create

    for manager in instances:
        _safe_shutdown(manager)


def _resolve_background_job_manager_classes() -> List[Type[Any]]:
    """Resolve every distinct `BackgroundJobManager` class object reachable
    in this test process: the canonical class plus the `src.`-prefixed
    alias class, forced via an unconditional `importlib.import_module(...)`
    -- see the module docstring for why this must never be a conditional
    `sys.modules.get(...)` lookup. Returns a single-element list if (for
    whatever reason, e.g. a future packaging change) the two names resolve
    to the identical class object.
    """
    from code_indexer.server.repositories import background_jobs as canonical_module

    classes: List[Type[Any]] = [canonical_module.BackgroundJobManager]

    alias_module = importlib.import_module(
        "src.code_indexer.server.repositories.background_jobs"
    )
    alias_class = alias_module.BackgroundJobManager
    if alias_class is not canonical_module.BackgroundJobManager:
        classes.append(alias_class)

    return classes


def _teardown_all_background_job_managers_impl(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """Core generator body for the `_teardown_all_background_job_managers`
    autouse fixture below, extracted as a plain function so it can be
    driven directly (via `next()`, with a manually constructed
    `pytest.MonkeyPatch()`) by dedicated unit tests without needing
    pytest's fixture machinery -- see
    tests/unit/server/test_background_job_manager_universal_teardown_1635.py.

    Monkeypatches `__init__` on every class from
    `_resolve_background_job_manager_classes()` to record every instance
    constructed while patched, then shuts each one down (best-effort,
    de-duplicated by `id()`) once the caller resumes the generator after
    its `yield`.
    """
    created_instances: List[Any] = []

    for cls in _resolve_background_job_manager_classes():
        original_init = cls.__init__

        def _tracking_init(
            self: Any,
            *args: Any,
            __original_init: Callable[..., None] = original_init,
            **kwargs: Any,
        ) -> None:
            # Bug #1635 (4th review remediation): track BEFORE calling the
            # original __init__, not after. BackgroundJobManager.__init__
            # starts its worker-thread pools near the very end of
            # construction (background_jobs.py); if thread.start() raises
            # partway through (e.g. thread exhaustion -- precisely the
            # failure mode this bug is about), any already-started threads
            # from that partially-constructed instance would never be
            # tracked and would leak untracked if appended only after a
            # successful return. All state shutdown() touches (_lock,
            # _running_jobs, jobs, _sqlite_backend, _pool_shutdown,
            # _background_jobs_config, the two pending-job queues) is
            # assigned earlier in __init__ than the thread-starting loops,
            # so shutdown() remains safe to call on a partially-constructed
            # instance; _safe_shutdown's broad except is the remaining
            # safety net for anything unforeseen.
            created_instances.append(self)
            __original_init(self, *args, **kwargs)

        monkeypatch.setattr(cls, "__init__", _tracking_init)

    yield

    seen_ids: Set[int] = set()
    for instance in created_instances:
        if id(instance) in seen_ids:
            continue
        seen_ids.add(id(instance))
        _safe_shutdown(instance)


@pytest.fixture(autouse=True)
def _teardown_all_background_job_managers() -> Generator[None, None, None]:
    """Bug #1635 (3rd review remediation): vector-agnostic universal
    teardown for every BackgroundJobManager constructed during a test,
    regardless of how it was constructed. See the module docstring above
    for the full rationale and the measured leak evidence this replaces.

    Bug #1635 (4th review remediation): constructs its own private
    `pytest.MonkeyPatch()` instead of requesting the shared `monkeypatch`
    fixture. Requesting the shared fixture from an autouse fixture makes
    pytest instantiate `monkeypatch` before every test's own explicitly
    requested fixtures; since fixture teardown runs in reverse-of-setup
    order, `monkeypatch.undo()` would then run AFTER every other fixture's
    finalizer instead of before it, silently inverting teardown ordering
    semantics for every test under tests/unit/server/. A private instance
    keeps this fixture's patch/undo lifecycle fully independent of
    pytest's fixture-dependency-graph ordering.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        yield from _teardown_all_background_job_managers_impl(monkeypatch)
    finally:
        monkeypatch.undo()


def _snapshot_restore_shared_app_state_impl() -> Generator[None, None, None]:
    """Core generator body for the `_snapshot_restore_shared_app_state`
    autouse fixture below, extracted as a plain function so it can be
    driven directly (via `next()`) by dedicated unit tests without needing
    pytest's fixture machinery -- see
    tests/unit/server/test_app_state_leak_protection_1694.py, mirroring
    the established `_teardown_all_background_job_managers_impl` pattern
    above.

    Bug #1694 (durable generalization of Bug #1675's per-file fix):
    several test files across this tree
    (`web/test_dependency_map_routes_sentinel.py`,
    `web/test_depmap_job_status_async.py`,
    `web/test_recent_run_metrics_template_bug_874.py` -- fixed narrowly
    for #1675 -- plus `routers/test_git_cat_endpoint.py`,
    `routers/test_git_file_history_endpoint.py`,
    `routers/test_git_blame_endpoint.py`,
    `routers/test_repos_sync_status_endpoint.py`, `test_custom_group_*.py`,
    `middleware/test_correlation_delegates_to_bridge_1632.py`, and
    `telemetry/test_request_tracing.py`) each run
    `with TestClient(app) as tc:` against the SHARED
    `code_indexer.server.app.app` singleton. That runs the REAL FastAPI
    lifespan, which wires real production services (e.g. a real
    `DependencyMapService` bound to this machine's actual golden-repos
    directory) onto `app.state`. Lifespan shutdown stops background
    threads/schedulers but never resets the app.state attributes it set --
    they stay bound to the process-wide `app` object and leak into every
    later test in the same pytest session that reads `app.state.*`,
    regardless of which file or directory it lives in.

    Rather than adding another per-file save/restore patch every time this
    shape is found (three times now: #1664, #1675, #1694), this generator
    snapshots the ENTIRE `app.state` backing dict before each test and
    restores it verbatim after -- closing the whole class of leak (any
    app.state.* attribute a real lifespan run happens to set, not just
    `dependency_map_service`) in one place.

    CRITICAL safety property (Bug #1638): this must NEVER be the thing
    that causes `code_indexer.server.app.app` to be constructed. `app` is
    lazily built via PEP 562 `__getattr__` (Bug #1638) precisely so a bare
    import stays inert; forcing construction here unconditionally, for
    EVERY test under tests/unit/server/ (this fixture is autouse across
    the whole ~14,500-test tree), would reintroduce exactly the
    import-time-service-construction regression #1638 fixed, plus real
    contention for the live local dev server's `primary_instance.lock`.
    So this checks `"app" in vars(_server_app_module)` -- a plain dict
    membership test that does NOT invoke `__getattr__` -- and no-ops
    entirely when `app` has not yet been constructed by anything else in
    the session. Every currently-known leaking test file already imports
    `app` at MODULE level (`from code_indexer.server.app import app`),
    which resolves during pytest's collection phase, before any fixture's
    setup runs -- so by the time this generator's setup phase executes for
    the first affected test, `app` is already present, and this snapshot
    guard is active for it.

    Ordering (function-scoped / in-test-body `TestClient(app)` usage ONLY):
    pytest runs autouse fixtures' setup BEFORE a test's own
    explicitly-requested fixtures (e.g. a file's `client`/`test_client`
    fixture) within the SAME scope, and tears down in reverse order -- so
    for a function-scoped `client`/`test_client` fixture, or a bare
    `with TestClient(app) as tc:` written directly in a test body, the
    snapshot here is taken before that lifespan context is entered, and
    the restore here runs AFTER that context has already exited (i.e.
    after real lifespan shutdown has run), which is exactly the ordering
    needed to undo whatever the lifespan mutated.

    KNOWN LIMITATION -- module/class/session-scoped `TestClient(app)`
    fixtures are NOT covered by this ordering guarantee, and this fixture
    does NOT clean up after them. A wider-scoped fixture's setup runs
    BEFORE this function-scoped autouse fixture's setup for the FIRST
    test collected in that scope (pytest always enters a broader-scoped
    fixture before a narrower-scoped one), so any `app.state` mutation
    made by that fixture's own lifespan entry is already baked into the
    very first snapshot this generator takes -- there is no "before" left
    to snapshot back to. Symmetrically, that fixture's teardown (and the
    lifespan shutdown inside it) runs AFTER this generator's own restore
    for the LAST test in that scope, so nothing here ever runs after it to
    undo it either. Three files in this tree currently rely on
    module-scoped `TestClient(app)` fixtures and fall squarely in this
    gap: `web/test_dependency_map_routes_sentinel.py`,
    `web/test_depmap_job_status_async.py`, and
    `web/test_recent_run_metrics_template_bug_874.py`. Despite this
    fixture's broader tree-wide reach, those three files' own #1675-style
    per-file save/restore patches are STILL REQUIRED protection for the
    module-scoped leak and are NOT made redundant by this fixture -- do
    not remove them.
    """
    if "app" not in vars(_server_app_module):
        # `app` singleton not yet constructed by anything in this pytest
        # session -- nothing to protect, and checking must never itself
        # trigger construction (see docstring above).
        yield
        return

    shared_app = _server_app_module.app
    snapshot = dict(shared_app.state._state)
    try:
        yield
    finally:
        state_dict = shared_app.state._state
        state_dict.clear()
        state_dict.update(snapshot)


@pytest.fixture(autouse=True)
def _snapshot_restore_shared_app_state() -> Generator[None, None, None]:
    """Bug #1694: tree-wide autouse fixture that snapshots and restores
    `code_indexer.server.app.app`'s `app.state` around every test in
    tests/unit/server/. See `_snapshot_restore_shared_app_state_impl`
    above for the full rationale.

    Purely additive protection for the real-lifespan-singleton leak
    pattern: a test that builds and tears down its own independent
    `app = create_app()` instance (a different object entirely) is
    untouched by this fixture, since only the shared module-level
    singleton object is ever snapshotted/restored here.
    """
    yield from _snapshot_restore_shared_app_state_impl()
