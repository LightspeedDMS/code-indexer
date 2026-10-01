"""
Unit tests for Bug #1801: ConfigService.get_config()'s check-then-act
lazy-init/reload sequence is not synchronized with the `_config_update_lock`
that update_settings_atomic() uses.

Root cause (proven via direct reproduction, not assumed):
get_config()'s lazy-init --

    def get_config(self) -> ServerConfig:
        if self._config is None:
            self.load_config()
        ...

-- delegates to load_config(), which unconditionally does
`self._config = <freshly read config>` on every call, with no locking at
all. A concurrent thread that merely READS config via get_config() -- e.g.
SystemMetricsCollector's leaked background refresh thread
(services/system_metrics_collector.py, thread name "SystemMetricsRefresh",
whose `_resolve_data_path()` calls `get_config_service().get_config()`
every ~5 seconds and is never torn down between tests) -- can observe
self._config is None on a freshly constructed ConfigService at the same
moment a real update is landing its first-ever write via
update_settings_atomic(), and its own lazily-triggered load_config() call
(reading disk BEFORE the write, but publishing self._config AFTER it)
silently reverts the just-applied setting back to bootstrap defaults.

This is a genuine PRODUCTION race in config_service.py, not merely a test
hygiene issue: a real leaked SystemMetricsCollector thread reproduces the
exact reported failure --
tests/unit/server/mcp/test_protocol_search_timeouts_config_1398.py::
TestResolveHandlerTimeoutReflectsLiveConfig::
test_search_code_timeout_reflects_configured_value -- failing intermittently
inside the full server-fast-automation.sh gate (chunk 5: mcp/ + telemetry/ +
handlers/), where temporal_inline_wait_seconds reverts from a just-applied
5.0 back to the 60.0 default between two update_setting() calls on the SAME
isolated ConfigService instance. Confirmed via a 3000-iteration statistical
stress harness spawning a real background thread that only calls
get_config_service().get_config() in a tight loop while the main thread
performs the exact two update_setting() calls from the failing test --
reproduced the identical ValueError ("temporal_inline_wait_seconds must be
<= search_code_handler_timeout_seconds - 1.0 (grace budget), got 60.0 ...")
in 3 of 3000 runs.

Fix direction: load_config() must acquire the SAME `_config_update_lock`
that update_settings_atomic() already uses, for its entire
read-merge-publish sequence. This makes the lazy-init race structurally
impossible for ALL callers (get_config()'s lazy path, and the handful of
direct load_config() callers in web/routes.py and startup/lifespan.py),
rather than patching only the specific test that happened to notice.

PRIMARY test design: stub the COLLABORATOR, not the SUT.
--------------------------------------------------------
load_config() calls `self.config_manager.load_config()`. `config_manager`
is a documented, injectable constructor parameter of ConfigService
("Optional pre-built ServerConfigManager instance ... Primarily useful for
unit tests") -- a legitimate seam the test owns, not an instrumentation of
ConfigService itself. `_ReaderParkingConfigManager` below wraps a REAL
ServerConfigManager and delegates every call to it unchanged, except that
load_config() called from one specific "reader" thread first performs the
REAL read (capturing whatever is on disk at that exact moment -- i.e. the
pre-write, stale snapshot), signals an Event proving it reached this exact
point, and then parks on a second Event before returning that captured
value. This forces, deterministically, the precise interleaving that loses
a concurrent write: the read happens before the write, but the publish
(load_config()'s return, and the subsequent `self._config = ...` inside
ConfigService's own, completely unmodified load_config() body) is held
back until the test releases it.

A separate "writer" thread performs the exact two update_setting() calls
from the originally failing test. It must run on its own thread rather
than the test's main thread: under the (now fixed) real implementation,
the parked reader holds `_config_update_lock` for the whole time it is
parked, so a writer attempted synchronously on the driving thread would
deadlock -- not a flaw in the fix, but the correct behavior of any
lock-based fix, which is exactly why a separate, joinable thread is used
here instead.

Ordering guarantee and its honestly-stated limit: the test releases the
parked reader only once the writer has EITHER (a) signalled completion --
detected via fast polling, so the reader is released within a millisecond
or two of the writer actually finishing, not after riding out a fixed
window -- or (b) a generous overall cap has elapsed. While parked, the
reader blocks on a real threading.Event.wait(), which releases the GIL
entirely, so (unlike an earlier, rejected busy-spin design) it never
competes with the writer for CPU; the writer's own work here is a handful
of dataclass copies, one validation, and one small tmp-file write,
performed twice, with nothing else contending for `_config_update_lock`
pre-fix. Waiting an UNBOUNDED amount for the writer before releasing the
reader was deliberately rejected: under any correct lock-based fix
(including the one landed here), the writer cannot make progress until
the reader is released, so an unbounded wait-then-release would deadlock
every correct implementation, not just this one. Given that constraint,
some non-zero (though vanishingly small under any conceivable scheduling
delay for genuinely unblocked, bounded, non-blocking-I/O work) residual
timing window is unavoidable for testing a REAL cross-thread race without
instrumenting production code -- the same class of trade-off already
accepted elsewhere in this codebase, e.g.
test_config_service_load_atomic.py's Bug #998 tests. The final assertion
is on the OBSERVABLE PUBLISHED STATE after both threads are joined, not on
timing, so it fails for the right reason (a stale value overwrote the
write) whenever that interleaving actually occurs, and passes for ANY
implementation that structurally prevents it -- a lock held across the
whole body (this fix), double-checked locking, an atomic/versioned
publish, or anything else -- not only this specific implementation.

SUPPLEMENTARY guard: `test_load_config_holds_config_update_lock_for_its_
entire_body_1801` below additionally pins THIS SPECIFIC fix's
implementation shape (the whole method body wrapped in exactly one
`with self._config_update_lock:`) via `ast` inspection. It is intentionally
narrower than the primary test above -- a different, equally valid fix
would legitimately turn it red without the underlying bug having
regressed -- so it exists only as a fast, zero-threading structural
tripwire for accidental drift in this file's own implementation, not as
the source of truth for whether the race is fixed.
"""

import ast
import inspect
import textwrap
import threading
import time
from typing import List, cast

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import ServerConfigManager

# Generous ceiling for thread join()s and for the reader's own internal
# park-wait -- never load-bearing for correctness (the final assertion is
# on published state, not timing), only a backstop against a genuinely
# hung/deadlocked run. Deliberately kept well ABOVE
# WRITER_COMPLETION_POLL_CAP_SECONDS below: the reader parks BEFORE the
# writer-completion poll even starts, so its own wait must never be able
# to expire before that poll's cap has had a chance to release it --
# otherwise the two independent timers race each other instead of the
# release genuinely driving the reader's wakeup.
THREAD_TIMEOUT_SECONDS = 10.0

# Overall cap on how long we poll for the writer to signal completion
# before unconditionally releasing the parked reader regardless. See the
# module docstring for why an unbounded wait here is not an option (it
# would deadlock any correct lock-based fix) and why this specific
# trade-off is the best achievable without instrumenting ConfigService.
WRITER_COMPLETION_POLL_CAP_SECONDS = 5.0
WRITER_COMPLETION_POLL_INTERVAL_SECONDS = 0.002


class _ReaderParkingConfigManager:
    """Test double for ServerConfigManager -- a genuine ConfigService
    collaborator injected via its own documented `config_manager=`
    constructor parameter, not a patch/mock of ConfigService itself.
    Delegates every call to a REAL ServerConfigManager unchanged, except
    that load_config() called from one designated "reader" thread captures
    the real read result immediately (before anything can have written
    concurrently) and then parks on a controlled Event before returning
    it, deterministically forcing the read-before-write,
    publish-after-write interleaving that loses a concurrent update."""

    def __init__(
        self,
        real_manager: ServerConfigManager,
        reader_thread_id: dict,
        entered_read: threading.Event,
        release_read: threading.Event,
    ) -> None:
        self._real_manager = real_manager
        self._reader_thread_id = reader_thread_id
        self._entered_read = entered_read
        self._release_read = release_read

    def load_config(self):
        if threading.get_ident() != self._reader_thread_id.get("id"):
            return self._real_manager.load_config()
        result = self._real_manager.load_config()  # capture BEFORE parking
        self._entered_read.set()
        if not self._release_read.wait(timeout=THREAD_TIMEOUT_SECONDS):
            raise RuntimeError(
                "test infrastructure failure: main thread never released "
                "the parked reader within the timeout"
            )
        return result

    def __getattr__(self, name):
        return getattr(self._real_manager, name)


def _run_reader(
    svc: ConfigService,
    reader_thread_id: dict,
    done: threading.Event,
    errors: list,
) -> None:
    reader_thread_id["id"] = threading.get_ident()  # own thread's first act
    try:
        svc.get_config()
        done.set()
    except BaseException as exc:  # noqa: BLE001 -- re-raised on main thread
        errors.append(exc)


def _run_writer(svc: ConfigService, done: threading.Event, errors: list) -> None:
    try:
        svc.update_setting("search_timeouts", "temporal_inline_wait_seconds", 5.0)
        svc.update_setting("search_timeouts", "search_code_handler_timeout_seconds", 35)
        done.set()
    except BaseException as exc:  # noqa: BLE001 -- re-raised on main thread
        errors.append(exc)


def _wait_for_writer_then_release(
    writer_done: threading.Event, release_read: threading.Event
) -> None:
    """Polls for writer completion at fine granularity so the reader is
    released within a couple of milliseconds of the writer actually
    finishing -- not after riding out a fixed window regardless -- while
    still capping total wait time so a genuinely blocked writer (the
    correct, expected outcome under any lock-based fix) does not hang the
    test. See the module docstring for why this specific shape (poll to
    release ASAP, bounded, always release in the end) is the closest
    achievable approximation of "prove completion, else release" without
    either deadlocking a correct fix or instrumenting ConfigService."""
    deadline = time.monotonic() + WRITER_COMPLETION_POLL_CAP_SECONDS
    while time.monotonic() < deadline:
        if writer_done.is_set():
            break
        time.sleep(WRITER_COMPLETION_POLL_INTERVAL_SECONDS)
    release_read.set()


def test_concurrent_get_config_cannot_clobber_in_flight_update_1801(tmp_path) -> None:
    """Behavioral regression for Bug #1801 -- see the module docstring for
    the full choreography and why it is deterministic (to the extent
    achievable without instrumenting or altering ConfigService itself)."""
    real_manager = ServerConfigManager(server_dir_path=str(tmp_path))
    reader_thread_id: dict = {}
    entered_read = threading.Event()
    release_read = threading.Event()
    stub_manager = _ReaderParkingConfigManager(
        real_manager, reader_thread_id, entered_read, release_read
    )
    # _ReaderParkingConfigManager duck-types ServerConfigManager's public
    # surface (delegating everything but load_config() via __getattr__) --
    # it is an intentional test double, not a real subclass, hence the cast.
    svc = ConfigService(config_manager=cast(ServerConfigManager, stub_manager))
    assert svc._config is None  # never loaded yet -- exercises the lazy path

    reader_done = threading.Event()
    reader_errors: list = []
    reader = threading.Thread(
        target=_run_reader,
        args=(svc, reader_thread_id, reader_done, reader_errors),
        daemon=True,
    )

    writer_done = threading.Event()
    writer_errors: list = []
    writer = threading.Thread(
        target=_run_writer, args=(svc, writer_done, writer_errors), daemon=True
    )

    try:
        reader.start()
        assert entered_read.wait(timeout=THREAD_TIMEOUT_SECONDS), (
            "reader thread never reached the stubbed read"
        )

        writer.start()
        _wait_for_writer_then_release(writer_done, release_read)
    finally:
        release_read.set()  # idempotent safety net if the block above raised
        reader.join(timeout=THREAD_TIMEOUT_SECONDS)
        writer.join(timeout=THREAD_TIMEOUT_SECONDS)

    assert not reader.is_alive(), "reader thread did not finish"
    assert not writer.is_alive(), "writer thread did not finish"
    if writer_errors:
        raise writer_errors[0]
    if reader_errors:
        raise reader_errors[0]
    assert reader_done.is_set(), "reader's get_config() never completed"
    assert writer_done.is_set(), "writer's update_setting() calls never completed"

    final = svc.get_config()
    assert (
        final.search_timeouts_config is not None
    )  # ServerConfig.__post_init__ guarantee
    assert final.search_timeouts_config.temporal_inline_wait_seconds == 5.0, (
        "a concurrent get_config() call (parked mid-read, then released "
        "after the write completed) clobbered the already-applied "
        "temporal_inline_wait_seconds update back to bootstrap defaults "
        "-- Bug #1801."
    )
    assert final.search_timeouts_config.search_code_handler_timeout_seconds == 35


# ---------------------------------------------------------------------------
# Supplementary structural guard (see module docstring: narrower than the
# behavioral test above, kept only as a fast implementation-shape tripwire
# for THIS fix, not as the source of truth for the race being fixed).
# ---------------------------------------------------------------------------


def _strip_leading_docstring(body: List[ast.stmt]) -> List[ast.stmt]:
    """Returns `body` with a leading bare docstring Expr node removed, if
    present, so the "entire operational body" check below isn't defeated
    by an otherwise-harmless docstring."""
    if not body:
        return body
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _is_self_config_update_lock(expr: ast.expr) -> bool:
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr == "_config_update_lock"
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "self"
    )


def _load_config_body_holds_config_update_lock() -> bool:
    """True iff ConfigService.load_config()'s (real, unmodified) source
    consists, after skipping an optional docstring, of EXACTLY ONE
    top-level statement: `with self._config_update_lock:` wrapping the
    method's entire operational body."""
    source = textwrap.dedent(inspect.getsource(ConfigService.load_config))
    tree = ast.parse(source)
    func_def = tree.body[0]
    assert isinstance(func_def, ast.FunctionDef), (
        "expected a single function definition"
    )

    body = _strip_leading_docstring(func_def.body)
    if len(body) != 1 or not isinstance(body[0], (ast.With, ast.AsyncWith)):
        return False

    with_stmt = body[0]
    return any(
        _is_self_config_update_lock(item.context_expr) for item in with_stmt.items
    )


def test_load_config_holds_config_update_lock_for_its_entire_body_1801() -> None:
    """Supplementary structural guard -- see module docstring. The
    BEHAVIORAL test above (test_concurrent_get_config_cannot_clobber_in_
    flight_update_1801) is the source of truth for Bug #1801."""
    assert _load_config_body_holds_config_update_lock(), (
        "ConfigService.load_config() does not wrap its entire operational "
        "body in `with self._config_update_lock:`. If this is because a "
        "different, equally valid fix was applied, delete or update this "
        "supplementary guard -- but confirm "
        "test_concurrent_get_config_cannot_clobber_in_flight_update_1801 "
        "still passes first (Bug #1801)."
    )
