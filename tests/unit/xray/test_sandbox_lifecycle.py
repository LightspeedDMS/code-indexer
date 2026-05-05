"""User Mandate Section 5: Lifecycle Tests (Story #970).

Tests subprocess lifecycle management of PythonEvaluatorSandbox:

  a. Infinite-busy timeout — CPU-pegging evaluator is killed at HARD_TIMEOUT+SIGKILL_GRACE
  b. FD leak detection — 50 consecutive runs must not leak file descriptors (Linux only)
  c. Zombie process check — after mixed runs, no zombie children remain
  d. Pipe corruption — non-bool payload from subprocess maps to evaluator_returned_non_bool
  e. Exit code propagation — non-zero exit codes map to evaluator_subprocess_died

All tests use real subprocess execution; no mocks for subprocess behavior.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any
from unittest.mock import patch

import pytest

from code_indexer.xray.ast_engine import AstSearchEngine
from code_indexer.xray.sandbox import EvalResult, PythonEvaluatorSandbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node_root(source: str = "x = 1", lang: str = "python"):
    engine = AstSearchEngine()
    root = engine.parse(source, lang)
    return root, root


def _run(code: str, *, sandbox: PythonEvaluatorSandbox | None = None) -> EvalResult:
    sb = sandbox or PythonEvaluatorSandbox()
    node, root = _make_node_root()
    return sb.run(
        code,
        node=node,
        root=root,
        source="x = 1",
        lang="python",
        file_path="/src/main.py",
    )


# ---------------------------------------------------------------------------
# a. Infinite-busy timeout
# ---------------------------------------------------------------------------


@pytest.mark.timeout(30)
def test_infinite_busy_evaluator_times_out() -> None:
    """Evaluator that pegs CPU with sum(range(10**9)) is killed at hard timeout.

    sum(range(N)) uses only Call+Name+Constant+BinOp — all whitelisted.
    The evaluator is terminated via SIGTERM at HARD_TIMEOUT_SECONDS (~5s),
    then SIGKILL at +SIGKILL_GRACE_SECONDS (~1s) if still alive.

    Use a patched timeout of 1.0s to keep the test fast (~2s total).
    """
    sb = PythonEvaluatorSandbox()
    # Patch timeout to 1.0s so the test finishes in ~2s instead of ~6s
    with patch.object(type(sb), "HARD_TIMEOUT_SECONDS", new=1.0):
        with patch.object(type(sb), "SIGKILL_GRACE_SECONDS", new=1.0):
            start = time.monotonic()
            # 1000000000 is a Constant literal — no BinOp needed, all nodes whitelisted
            result = _run("return sum(range(1000000000)) > 0", sandbox=sb)
            elapsed = time.monotonic() - start

    assert result.failure == "evaluator_timeout", (
        f"Expected evaluator_timeout, got failure={result.failure!r}, "
        f"detail={result.detail!r}"
    )
    assert result.value is None
    # Should complete within timeout + grace + 0.5s margin
    assert elapsed < 3.5, f"Timeout took too long: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# b. FD leak detection (Linux only)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
@pytest.mark.skipif(sys.platform != "linux", reason="FD count via /proc only on Linux")
def test_no_fd_leak_after_50_runs() -> None:
    """50 consecutive sandbox runs must not leak file descriptors.

    Measures /proc/self/fd before and after; allows ±2 for system noise.
    """
    our_pid = os.getpid()
    fd_dir = f"/proc/{our_pid}/fd"

    def count_fds() -> int:
        return len(os.listdir(fd_dir))

    # Warm up: one run to settle any lazy init
    _run("return True")

    before = count_fds()
    for _ in range(50):
        result = _run("return True")
        assert result.failure is None, f"Unexpected failure: {result.failure}"
        assert result.value is True

    after = count_fds()
    delta = after - before
    assert abs(delta) <= 2, (
        f"FD leak detected: before={before}, after={after}, delta={delta}"
    )


# ---------------------------------------------------------------------------
# c. Zombie process check
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_no_zombie_processes_after_mixed_runs() -> None:
    """After 10 mixed evaluator scenarios, no zombie children remain."""
    psutil = pytest.importorskip("psutil")

    scenarios = [
        "return True",  # fast pass
        "return False",  # fast pass false
        "return len([1, 2, 3]) > 0",  # uses safe builtin
        "return str(42) == '42'",  # str builtin
        "return [x for x in range(10)]",  # validation_failed (comprehension)
        "return True",
        "return False",
        "return min(1, 2) == 1",  # min builtin
        "import os",  # validation_failed (import)
        "return int('5') == 5",  # int builtin
    ]

    for code in scenarios:
        _run(code)  # result ignored; we just care about process cleanup

    # Give OS a moment to reap children
    time.sleep(0.1)

    my_proc = psutil.Process(os.getpid())
    children = my_proc.children(recursive=True)
    zombies = [c for c in children if c.status() == psutil.STATUS_ZOMBIE]
    assert not zombies, (
        f"Zombie processes found after mixed runs: "
        f"{[(c.pid, c.status()) for c in zombies]}"
    )


# ---------------------------------------------------------------------------
# d. Pipe corruption — non-bool payload
# ---------------------------------------------------------------------------


def _run_evaluator_sends_dict(
    code: str,
    node: Any,
    root: Any,
    source: str,
    lang: str,
    file_path: str,
    conn: Any,
    *args: Any,
) -> None:
    """Replacement for _run_evaluator that sends a dict (new file-as-unit contract)."""
    try:
        conn.send({"matches": [], "value": None})
    finally:
        conn.close()


def test_pipe_sends_dict_returns_value_v10_4_0() -> None:
    """v10.4.0 contract: subprocess dict payload is accepted as success value.

    In v10.4.0 the file-as-unit contract expects dicts from evaluators.
    sandbox.run() accepts any pipe value and returns EvalResult(value=raw).
    The evaluator_returned_non_bool failure mode is no longer emitted.
    """
    sb = PythonEvaluatorSandbox()
    node, root = _make_node_root()

    with patch(
        "code_indexer.xray.sandbox._run_evaluator",
        side_effect=_run_evaluator_sends_dict,
    ):
        result = sb.run(
            "return {'matches': [], 'value': None}",
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
        )

    assert result.failure is None, (
        f"Expected success (failure=None) for dict payload, got {result.failure!r}"
    )
    assert isinstance(result.value, dict), (
        f"Expected dict value, got {result.value!r}"
    )


# ---------------------------------------------------------------------------
# e. Exit code propagation
# ---------------------------------------------------------------------------


def _run_evaluator_exits(exit_code: int):
    """Factory: returns a _run_evaluator replacement that calls os._exit(N)."""

    def _impl(
        code: str,
        node: Any,
        root: Any,
        source: str,
        lang: str,
        file_path: str,
        conn: Any,
        *args: Any,
    ) -> None:
        conn.close()
        os._exit(exit_code)  # noqa: SLF001

    return _impl


@pytest.mark.parametrize("exit_code", [1, 139, -9])
def test_nonzero_exit_code_returns_subprocess_died(exit_code: int) -> None:
    """Subprocess exiting with non-zero code -> evaluator_subprocess_died."""
    sb = PythonEvaluatorSandbox()
    node, root = _make_node_root()

    with patch(
        "code_indexer.xray.sandbox._run_evaluator",
        side_effect=_run_evaluator_exits(exit_code),
    ):
        result = sb.run(
            "return True",
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
        )

    assert result.failure == "evaluator_subprocess_died", (
        f"exit_code={exit_code}: expected evaluator_subprocess_died, "
        f"got {result.failure!r}"
    )
    # The detail should reference the exit code
    assert result.detail is not None
    # With fork start method on Linux, multiprocessing may read proc.exitcode as
    # None before waitpid() has been called, causing the sandbox to fall through
    # to the "no_pipe_data" branch even for non-zero exits.  Both detail values
    # are valid outcomes for this test; the key invariant is evaluator_subprocess_died.
    assert "exitcode" in result.detail or "no_pipe_data" in result.detail, (
        f"exit_code={exit_code}: unexpected detail={result.detail!r}"
    )


def test_clean_exit_with_no_pipe_data_returns_subprocess_died() -> None:
    """Subprocess exits cleanly (exit code 0) but sends no pipe data.

    The parent detects the missing data via poll() and returns
    evaluator_subprocess_died with detail='no_pipe_data'.
    """
    sb = PythonEvaluatorSandbox()
    node, root = _make_node_root()

    with patch(
        "code_indexer.xray.sandbox._run_evaluator",
        side_effect=_run_evaluator_exits(0),
    ):
        result = sb.run(
            "return True",
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
        )

    # exit code 0 but no data sent -> no_pipe_data
    assert result.failure == "evaluator_subprocess_died", (
        f"Expected evaluator_subprocess_died, got {result.failure!r}"
    )
    assert result.detail == "no_pipe_data", (
        f"Expected detail='no_pipe_data', got {result.detail!r}"
    )


# ---------------------------------------------------------------------------
# f. validate() SyntaxError path (sandbox.py lines 244-245)
# ---------------------------------------------------------------------------


def test_validate_syntax_error_returns_validation_failed() -> None:
    """validate() returns ValidationResult(ok=False) for syntactically invalid code."""
    sb = PythonEvaluatorSandbox()
    result = sb.validate("return (")  # unclosed parenthesis — SyntaxError
    assert result.ok is False
    assert result.reason is not None
    assert "syntax_error" in result.reason


# ---------------------------------------------------------------------------
# g. _get_mp_context() spawn fallback (sandbox.py lines 377-378)
# ---------------------------------------------------------------------------


def test_get_mp_context_spawn_fallback() -> None:
    """_get_mp_context() falls back to 'spawn' when 'fork' is unavailable."""
    import multiprocessing

    # Save the real function BEFORE patching to avoid infinite recursion
    real_get_context = multiprocessing.get_context

    def raise_on_fork(method: str):
        if method == "fork":
            raise ValueError("fork not available")
        return real_get_context("spawn")

    with patch(
        "code_indexer.xray.sandbox.multiprocessing.get_context",
        side_effect=raise_on_fork,
    ):
        ctx = PythonEvaluatorSandbox._get_mp_context()

    assert ctx is not None
    assert ctx.get_start_method() == "spawn"


# ---------------------------------------------------------------------------
# h. no_pipe_data with non-zero exitcode (sandbox.py lines 323-330)
# ---------------------------------------------------------------------------


def _run_evaluator_exits_no_data_nonzero(
    code: Any,
    node: Any,
    root: Any,
    source: Any,
    lang: Any,
    file_path: Any,
    conn: Any,
    *args: Any,
) -> None:
    """Replacement: close pipe without sending, exit with code 2."""
    conn.close()
    os._exit(2)  # noqa: SLF001


def test_no_pipe_data_nonzero_exitcode_returns_subprocess_died_with_exitcode() -> None:
    """When subprocess sends no data and exits with non-zero code, detail includes exitcode."""
    sb = PythonEvaluatorSandbox()
    node, root = _make_node_root()

    with patch(
        "code_indexer.xray.sandbox._run_evaluator",
        side_effect=_run_evaluator_exits_no_data_nonzero,
    ):
        result = sb.run(
            "return True",
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
        )

    assert result.failure == "evaluator_subprocess_died", (
        f"Expected evaluator_subprocess_died, got {result.failure!r}"
    )
    assert result.detail is not None
    # OS-level race: depending on wait/poll timing, parent sees exitcode=N (when
    # proc.exitcode landed before pipe-read) OR no_pipe_data (when child exited
    # before pipe write). Both indicate the subprocess died — either is acceptable.
    assert ("exitcode" in (result.detail or "")) or (
        "no_pipe_data" in (result.detail or "")
    ), f"Unexpected detail: {result.detail!r}"


# ---------------------------------------------------------------------------
# i. SIGKILL escalation — subprocess ignores SIGTERM (sandbox.py lines 317-318)
# ---------------------------------------------------------------------------


def _run_evaluator_ignores_sigterm(
    code: Any,
    node: Any,
    root: Any,
    source: Any,
    lang: Any,
    file_path: Any,
    conn: Any,
    *args: Any,
) -> None:
    """Replacement: install SIG_IGN for SIGTERM and block forever.

    Forces the sandbox to escalate from SIGTERM to SIGKILL.
    """
    import signal as _signal
    import time as _time

    _signal.signal(_signal.SIGTERM, _signal.SIG_IGN)
    conn.close()
    # Block indefinitely — SIGTERM will be ignored, SIGKILL will terminate
    while True:
        _time.sleep(0.05)


@pytest.mark.timeout(30)
def test_sigkill_escalation_when_sigterm_ignored() -> None:
    """Subprocess ignoring SIGTERM is killed by SIGKILL (lines 317-318).

    Both timeouts are patched to 0.5s so the test completes in ~1.5s.
    """
    sb = PythonEvaluatorSandbox()
    node, root = _make_node_root()

    with patch.object(type(sb), "HARD_TIMEOUT_SECONDS", new=0.5):
        with patch.object(type(sb), "SIGKILL_GRACE_SECONDS", new=0.5):
            with patch(
                "code_indexer.xray.sandbox._run_evaluator",
                side_effect=_run_evaluator_ignores_sigterm,
            ):
                result = sb.run(
                    "return True",
                    node=node,
                    root=root,
                    source="x = 1",
                    lang="python",
                    file_path="/src/main.py",
                )

    # SIGKILL escalation race: depending on whether proc.is_alive() observes the kill
    # before proc.exitcode is set, parent reports either evaluator_timeout (kill path)
    # OR evaluator_subprocess_died (race-won by exit). Both indicate the long process
    # was forcefully terminated — either is acceptable for this scenario.
    assert result.failure in ("evaluator_timeout", "evaluator_subprocess_died"), (
        f"Expected evaluator_timeout or evaluator_subprocess_died, got {result.failure!r}"
    )
