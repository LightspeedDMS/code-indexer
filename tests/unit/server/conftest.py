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

from code_indexer.server.repositories.background_jobs import BackgroundJobManager

logger = logging.getLogger(__name__)


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
