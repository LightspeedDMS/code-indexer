"""R2-4 (Codex re-review): compile-concurrency semaphore.

The graph admission limiter (H8, Issue #1811/Bug #1812) bounds concurrent
GRAPH JOBS, but nothing previously bounded concurrent COMPILES
specifically -- multiple users triggering many simultaneous evaluator
compiles (each a real rustc process) can exhaust CPU/RAM/temp-disk/NFS at
fleet scale (~900 repos). This adds a process-wide, deadline-aware
semaphore acquired around every xray-cli invocation that can trigger a
real compile, released in `finally`.

Tests use REAL threads (no mocks) to prove genuine mutual exclusion and
deadline-respecting behavior -- not just that a semaphore object exists.
"""

from __future__ import annotations

import inspect
import threading
import time

from code_indexer.xray import rust_backend

# Named timing/workload constants (all bounded -- never open-ended waits).
_ACQUIRE_TIMEOUT_SECONDS = 1.0  # generous budget for filling real slots
_EXHAUSTED_ACQUIRE_TIMEOUT_SECONDS = 0.3  # short: proves deadline-awareness
_EXHAUSTED_ACQUIRE_MAX_ELAPSED_SECONDS = 2.0  # upper bound on how late it may return
_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS = 5.0  # per-acquire budget under contention
_CONCURRENCY_HOLD_SECONDS = 0.005  # brief hold to create real overlap
_CONCURRENCY_THREAD_MULTIPLIER = 3  # thread_count = bound * this
_CONCURRENCY_ITERATIONS_PER_THREAD = 20
_THREAD_JOIN_TIMEOUT_SECONDS = 30.0


def test_max_concurrent_compiles_constant_is_a_positive_bound() -> None:
    """Sanity: the configured bound is a real, positive integer -- not
    accidentally zero/unbounded."""
    assert isinstance(rust_backend._MAX_CONCURRENT_COMPILES, int)
    assert rust_backend._MAX_CONCURRENT_COMPILES > 0


# R3-4 (Codex re-review, ROUND 3): the concurrency tests below compare
# OBSERVED behavior only against the RUNTIME constant -- if someone
# silently raised _MAX_CONCURRENT_COMPILES to, say, 1000, those tests
# would adapt and still pass, masking a real regression in the intended
# admission-control bound. This pins the INTENDED value explicitly and
# separately.
_EXPECTED_MAX_CONCURRENT_COMPILES = 4


def test_max_concurrent_compiles_pins_the_intended_value() -> None:
    assert rust_backend._MAX_CONCURRENT_COMPILES == _EXPECTED_MAX_CONCURRENT_COMPILES, (
        f"_MAX_CONCURRENT_COMPILES changed from the intended "
        f"{_EXPECTED_MAX_CONCURRENT_COMPILES} to "
        f"{rust_backend._MAX_CONCURRENT_COMPILES} -- if this is a "
        f"deliberate change, update _EXPECTED_MAX_CONCURRENT_COMPILES in "
        f"this test file too, not silently"
    )


def test_acquire_compile_slot_parameter_has_no_default() -> None:
    """`_run_xray_cli_process`'s `acquire_compile_slot` parameter must be
    REQUIRED (no default) -- so every call site is forced to state its
    intent explicitly, and a future call site cannot silently inherit an
    unbounded/unthrottled default."""
    sig = inspect.signature(rust_backend.RustNativeBackend._run_xray_cli_process)
    assert "acquire_compile_slot" in sig.parameters, (
        "_run_xray_cli_process must declare an acquire_compile_slot parameter"
    )
    param = sig.parameters["acquire_compile_slot"]
    assert param.default is inspect.Parameter.empty, (
        "acquire_compile_slot must have NO default -- every call site must "
        f"pass it explicitly; got default={param.default!r}"
    )


def test_acquire_compile_slot_respects_deadline_when_exhausted() -> None:
    """When every slot is already held, `_acquire_compile_slot` must
    return False promptly once the given timeout elapses -- never block
    forever waiting for a slot that may never free up."""
    held_count = 0
    try:
        for _ in range(rust_backend._MAX_CONCURRENT_COMPILES):
            acquired = rust_backend._acquire_compile_slot(
                timeout_seconds=_ACQUIRE_TIMEOUT_SECONDS
            )
            assert acquired, "expected to be able to fill all real slots"
            held_count += 1

        start = time.monotonic()
        timed_out = rust_backend._acquire_compile_slot(
            timeout_seconds=_EXHAUSTED_ACQUIRE_TIMEOUT_SECONDS
        )
        elapsed = time.monotonic() - start

        assert timed_out is False, (
            "acquiring beyond the configured bound must fail, not silently succeed"
        )
        assert elapsed < _EXHAUSTED_ACQUIRE_MAX_ELAPSED_SECONDS, (
            f"_acquire_compile_slot must respect its own timeout budget "
            f"(deadline-aware) -- took {elapsed:.2f}s for a "
            f"{_EXHAUSTED_ACQUIRE_TIMEOUT_SECONDS}s timeout"
        )
    finally:
        for _ in range(held_count):
            rust_backend._release_compile_slot()


def test_compile_slot_enforces_real_mutual_exclusion_under_concurrency() -> None:
    """Real multi-threaded proof: hammer acquire/release from many threads
    and confirm the OBSERVED concurrent-holder count (a plain shared
    counter incremented/decremented around the critical section) never
    exceeds the configured bound. Worker exceptions are collected and
    re-raised so a silent per-thread assertion failure cannot be masked
    by only checking that threads finished."""
    active = {"count": 0}
    max_seen = {"value": 0}
    lock = threading.Lock()
    errors: list[BaseException] = []
    thread_count = (
        rust_backend._MAX_CONCURRENT_COMPILES * _CONCURRENCY_THREAD_MULTIPLIER
    )

    def worker() -> None:
        try:
            for _ in range(_CONCURRENCY_ITERATIONS_PER_THREAD):
                acquired = rust_backend._acquire_compile_slot(
                    timeout_seconds=_CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS
                )
                assert acquired, "should always eventually acquire under this workload"
                try:
                    with lock:
                        active["count"] += 1
                        max_seen["value"] = max(max_seen["value"], active["count"])
                    time.sleep(_CONCURRENCY_HOLD_SECONDS)
                    with lock:
                        active["count"] -= 1
                finally:
                    rust_backend._release_compile_slot()
        except BaseException as exc:  # noqa: BLE001 -- must capture ANY failure
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
        assert not t.is_alive(), (
            f"worker thread did not finish within {_THREAD_JOIN_TIMEOUT_SECONDS}s"
        )

    assert not errors, f"worker thread(s) raised: {errors!r}"
    assert max_seen["value"] <= rust_backend._MAX_CONCURRENT_COMPILES, (
        f"observed {max_seen['value']} concurrent compile-slot holders, "
        f"exceeding the configured bound of {rust_backend._MAX_CONCURRENT_COMPILES}"
    )


def test_legacy_scan_call_site_opts_into_compile_slot() -> None:
    """The legacy scan invocation (the only xray-cli invocation shape that
    can trigger a FRESH compile from run_batch) must pass
    acquire_compile_slot=True to _run_xray_cli_process."""
    source = inspect.getsource(rust_backend.RustNativeBackend._invoke_xray_cli)
    assert "acquire_compile_slot=True" in source, (
        "the legacy scan call site (_invoke_xray_cli) must request a "
        "compile slot -- it is the invocation shape that can trigger a "
        "fresh rustc compile"
    )


def test_graph_subcommand_call_site_explicitly_opts_out_of_compile_slot() -> None:
    """--build-graph/--analyze-graph run an ALREADY-compiled .so (no fresh
    compile) -- they must EXPLICITLY pass acquire_compile_slot=False, not
    merely omit the argument (which would rely on a default that could
    silently drift later). _run_xray_cli_process's parameter is required
    (no default) precisely so every call site states its intent."""
    source = inspect.getsource(rust_backend.RustNativeBackend._run_graph_subcommand)
    assert "acquire_compile_slot=False" in source, (
        "_run_graph_subcommand must explicitly pass acquire_compile_slot=False "
        "-- it runs a pre-compiled dylib, never triggers rustc"
    )
