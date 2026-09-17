"""
Unit tests for GitHub issue #1889: ConfigService.get_config()'s lazy-init
check (`if self._config is None: self.load_config()`) is not synchronized,
so two threads racing to lazily initialize the SAME freshly-constructed
ConfigService instance can BOTH decide to call load_config(), producing TWO
independently-parsed ServerConfig objects and silently detaching whichever
caller captured the FIRST object's reference from `self._config`. This is a
distinct gap from Bug #1801 (which locked load_config()'s entire body
against a CONCURRENT WRITER via update_settings_atomic(), but did nothing
to stop a SECOND concurrent LAZY-INIT READER from redundantly re-entering
load_config() and re-publishing a brand-new object).

Root cause (proven via direct reproduction with a real leaked
SystemMetricsCollector background thread): SystemMetricsCollector's
`SystemMetricsRefresh` daemon thread (constructed once, at the first real
`code_indexer.server.app` import anywhere in a pytest session, and never
torn down between tests) calls `get_config_service().get_config()` every
~5 seconds via `_resolve_data_path()`. When this tick lands during a
test's FIRST access to a brand-new, per-test ConfigService instance (e.g.
right after `reset_config_service()` in a test's own setup_method), both
the test's own thread and SystemMetricsRefresh can observe
`self._config is None` concurrently and both call `load_config()`.
Because `load_config()` (fixed for Bug #1801 to lock its own body)
unconditionally re-parses config.json and does `self._config = <fresh
object>` on EVERY invocation -- with no "someone else already loaded this"
check -- a redundant second call silently replaces `self._config` with a
DIFFERENT, freshly-deserialized ServerConfig object. A caller that
captured the FIRST object's reference (e.g.
tests/unit/server/services/test_file_service_token_enforcement.py's
`_update_config()` helper, holding `config = config_service.get_config()`
before mutating `config.content_limits_config.file_content_max_tokens`)
keeps mutating and persisting-to-disk ITS OWN (now detached) object, while
`self._config` on the shared ConfigService instance silently reverts to
whatever the SECOND, redundant load produced -- reproducing the exact
intermittent "truncated=False when it should be True" failures reported
in issue #1889
(test_file_service_skip_truncation.py::test_skip_truncation_default_is_false
and
test_file_service_token_enforcement.py::test_token_enforcement_truncates_large_content).

Fix direction: `get_config()`'s already-loaded fast path stays lock-free
(`if self._config is None:` outside the lock, for the hot common case).
Only the first-load path acquires `_config_update_lock` and re-checks
`self._config is None` INSIDE the lock before calling `load_config()`, so
a second thread arriving after the first has already published
`self._config` observes it is no longer None and never calls
`load_config()` a second time.

Determinism note: the interleaving this test forces does NOT rely on
sleep-based timing luck. Thread A's stubbed first `load_config()` call
parks while HOLDING `_config_update_lock` (real production behavior since
Bug #1801). Thread B is release-gated on a proven fact, not a guess: this
test wraps `svc._config_update_lock` in `_AcquireSignalingLock`, a thin
delegate that fires an Event the instant ANY thread CALLS `acquire()` on
it -- before it can block. Thread B's OWN `get_config()` call, under
EITHER the pre-fix or post-fix implementation, must reach an attempt to
acquire this same lock (buggy code: inside its own `load_config()` call;
fixed code: inside `get_config()`'s own double-checked-locking guard)
after seeing `self._config is None` -- so waiting for that second
`acquire()` attempt before releasing thread A is a genuine proof of
interleaving, not a timing assumption.
"""

import threading
from typing import List, Optional, cast

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import ServerConfig, ServerConfigManager

# Generous ceiling for thread join()s / internal park waits -- never
# load-bearing for correctness (the final assertions are on observed call
# counts and object identity, not on timing), only a backstop against a
# genuinely hung/deadlocked run.
THREAD_TIMEOUT_SECONDS = 10.0


class _CountingParkingConfigManager:
    """Test double for ServerConfigManager -- delegates every call to a
    REAL ServerConfigManager unchanged, except load_config() counts its
    own invocations and, on the FIRST call only, parks (blocks) after
    performing the real read but before returning -- giving a second,
    concurrent lazy-init caller a genuine window to also observe
    `self._config is None` and attempt its own get_config(), forcing the
    exact interleaving issue #1889 requires, deterministically (see
    `_AcquireSignalingLock` for how the SECOND caller's arrival is proven
    rather than assumed)."""

    def __init__(
        self,
        real_manager: ServerConfigManager,
        entered_first_load: threading.Event,
        release_first_load: threading.Event,
    ) -> None:
        self._real_manager = real_manager
        self._entered_first_load = entered_first_load
        self._release_first_load = release_first_load
        self.load_config_call_count = 0
        self._count_lock = threading.Lock()

    def load_config(self) -> Optional[ServerConfig]:
        with self._count_lock:
            self.load_config_call_count += 1
            is_first = self.load_config_call_count == 1
        result = self._real_manager.load_config()
        if is_first:
            self._entered_first_load.set()
            if not self._release_first_load.wait(timeout=THREAD_TIMEOUT_SECONDS):
                raise RuntimeError(
                    "test infrastructure failure: main thread never released "
                    "the parked first load_config() call within the timeout"
                )
        return result

    # Duck-typed passthrough for every other ServerConfigManager attribute
    # (e.g. create_default_config(), save_config(), server_dir) -- `Any`
    # return is unavoidable here since this proxies an arbitrary attribute
    # of the wrapped real manager, mirroring the identical, unannotated
    # pattern already established by
    # test_config_service_get_config_race_1801.py's own
    # `_ReaderParkingConfigManager.__getattr__`.
    def __getattr__(self, name: str):
        return getattr(self._real_manager, name)


class _AcquireSignalingLock:
    """Delegate wrapper around a real threading.RLock that fires an Event
    the instant `acquire()` is CALLED on it -- before the call can block.
    Used to deterministically prove a second thread has reached the point
    of attempting to acquire `_config_update_lock`, without any
    sleep-based timing assumption. Supports the same `with lock:` /
    reentrant usage ConfigService relies on, since it is a thin delegate
    over a real `threading.RLock`."""

    def __init__(
        self, real_lock: threading.RLock, on_acquire_attempt: threading.Event
    ) -> None:
        self._real_lock = real_lock
        self._on_acquire_attempt = on_acquire_attempt

    def acquire(self, *args, **kwargs) -> bool:
        self._on_acquire_attempt.set()
        return self._real_lock.acquire(*args, **kwargs)

    def release(self, *args, **kwargs) -> None:
        self._real_lock.release(*args, **kwargs)

    def __enter__(self) -> "_AcquireSignalingLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def _run_get_config(
    svc: ConfigService,
    results: List[Optional[ServerConfig]],
    errors: List[Optional[BaseException]],
    index: int,
) -> None:
    """Runs on its own thread; writes ONLY to its own index of `results`/
    `errors` (pre-sized lists, one slot per thread) so no shared mutable
    append is needed across threads."""
    try:
        results[index] = svc.get_config()
    except BaseException as exc:  # noqa: BLE001 -- captured for main-thread re-raise
        errors[index] = exc


def test_concurrent_lazy_init_calls_load_config_exactly_once_1889(tmp_path) -> None:
    """Two threads racing get_config() on a brand-new ConfigService (never
    loaded yet) must trigger exactly ONE call to the underlying
    config_manager.load_config() -- proving the second racer's lazy-init
    check is synchronized against the first, rather than independently
    re-parsing config.json and silently detaching self._config from
    whatever the first caller already captured (issue #1889)."""
    real_manager = ServerConfigManager(server_dir_path=str(tmp_path))
    entered_first_load = threading.Event()
    release_first_load = threading.Event()
    stub_manager = _CountingParkingConfigManager(
        real_manager, entered_first_load, release_first_load
    )
    svc = ConfigService(config_manager=cast(ServerConfigManager, stub_manager))
    assert svc._config is None  # never loaded yet -- exercises the lazy path

    second_acquire_attempted = threading.Event()
    svc._config_update_lock = cast(
        threading.RLock,
        _AcquireSignalingLock(svc._config_update_lock, second_acquire_attempted),
    )

    results: List[Optional[ServerConfig]] = [None, None]
    errors: List[Optional[BaseException]] = [None, None]

    thread_a = threading.Thread(
        target=_run_get_config, args=(svc, results, errors, 0), daemon=True
    )
    thread_b = threading.Thread(
        target=_run_get_config, args=(svc, results, errors, 1), daemon=True
    )

    try:
        thread_a.start()
        assert entered_first_load.wait(timeout=THREAD_TIMEOUT_SECONDS), (
            "thread A never reached the stubbed (parked) first load_config() call"
        )
        # self._config must still be unpublished while thread A is parked --
        # this is what makes thread B's own lazy-init check a genuine race,
        # exactly like a leaked SystemMetricsCollector background thread.
        assert svc._config is None, (
            "test setup invariant violated: self._config must still be "
            "unpublished while thread A is parked"
        )

        # Thread A's FIRST acquire() (already made, to reach the park) has
        # already fired the signal once; clear it so we can wait for
        # thread B's OWN, later, second attempt specifically.
        second_acquire_attempted.clear()

        thread_b.start()
        assert second_acquire_attempted.wait(timeout=THREAD_TIMEOUT_SECONDS), (
            "thread B never attempted to acquire _config_update_lock -- "
            "test infrastructure did not observe the race window"
        )

        release_first_load.set()
    finally:
        release_first_load.set()  # idempotent safety net if the block above raised
        thread_a.join(timeout=THREAD_TIMEOUT_SECONDS)
        thread_b.join(timeout=THREAD_TIMEOUT_SECONDS)

    assert not thread_a.is_alive(), "thread A did not finish"
    assert not thread_b.is_alive(), "thread B did not finish"
    for err in errors:
        if err is not None:
            raise err

    assert stub_manager.load_config_call_count == 1, (
        "get_config()'s lazy-init check is not synchronized: a second "
        "concurrent caller triggered a REDUNDANT config_manager.load_config() "
        f"call (count={stub_manager.load_config_call_count}), which silently "
        "re-parses config.json into a NEW object and detaches self._config "
        "from whatever the first caller already captured -- issue #1889."
    )
    assert results[0] is results[1] is svc._config, (
        "both concurrent get_config() callers must observe the SAME "
        "published ServerConfig object"
    )
