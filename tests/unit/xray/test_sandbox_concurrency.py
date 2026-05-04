"""User Mandate Section 6: Concurrency Tests (Story #970).

Tests concurrent execution of PythonEvaluatorSandbox:

  a. 50 evaluator runs (30 fast-pass + 15 fast-reject + 5 timeout-bound)
     submitted truly concurrently to a 20-worker pool.
     Wall clock must be approximately max(timeout) ≈ 1.5s, NOT sum(timeouts).
     Concurrency proof: elapsed < 5.0s with 5 timeout tasks at 1.0s each.

  b. Memory under load: 50 sequential fast-pass runs, RSS delta <100MB.

Thread-safety invariant: each run() call spawns an independent subprocess
with its own Pipe; no shared mutable state between workers.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from code_indexer.xray.ast_engine import AstSearchEngine
from code_indexer.xray.sandbox import EvalResult, PythonEvaluatorSandbox
from code_indexer.xray import sandbox as sb_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node_root(source: str = "x = 1", lang: str = "python"):
    engine = AstSearchEngine()
    root = engine.parse(source, lang)
    return root, root


def _run_with_sandbox(sandbox: PythonEvaluatorSandbox, code: str) -> EvalResult:
    """Run code with the given sandbox instance."""
    node, root = _make_node_root()
    return sandbox.run(
        code,
        node=node,
        root=root,
        source="x = 1",
        lang="python",
        file_path="/src/main.py",
    )


# ---------------------------------------------------------------------------
# a. 50 evaluator runs truly concurrent — all three outcome types
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
def test_50_mixed_evaluators_truly_concurrent(monkeypatch) -> None:
    """All 50 tasks (30 fast-pass + 15 fast-reject + 5 timeout-bound) submitted
    concurrently to a 20-worker pool. Wall clock should be approximately
    max(timeout) ≈ 1.5s — NOT sum(timeouts) which would indicate sequential.

    Concurrency proof: with 20-worker pool and 5 timeout tasks at 1.0s each,
    truly concurrent execution finishes in ~1.5-3s total.
    Sequential would take 5 * 1.0s = 5s+ for the timeouts alone, pushing
    total elapsed above 5s.
    """
    monkeypatch.setattr(sb_mod.PythonEvaluatorSandbox, "HARD_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(sb_mod.PythonEvaluatorSandbox, "SIGKILL_GRACE_SECONDS", 0.5)
    sb = sb_mod.PythonEvaluatorSandbox()

    engine = AstSearchEngine()

    tasks: list[tuple[str, str | None]] = []
    tasks.extend([("return True", None)] * 30)
    tasks.extend([("import os", "validation_failed")] * 15)
    tasks.extend([("return sum(range(1000000000)) > 0", "evaluator_timeout")] * 5)

    def _run_task(entry: tuple[str, str | None]) -> tuple[EvalResult, str | None]:
        code, expected = entry
        root = engine.parse("x = 1", "python")
        result = sb.run(
            code,
            node=root,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/tmp/x.py",
        )
        return result, expected

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(_run_task, task) for task in tasks]
        pairs = [f.result() for f in as_completed(futures)]
    elapsed = time.monotonic() - start

    # Concurrency proof: truly concurrent execution with 5 timeout tasks at 1.0s
    # should finish well under 5s total.
    assert elapsed < 5.0, (
        f"Tests not truly concurrent: elapsed={elapsed:.2f}s — "
        "expected < 5.0s with 20-worker pool and 1.0s timeouts"
    )

    pass_count = sum(1 for r, _ in pairs if r.failure is None and r.value is True)
    reject_count = sum(1 for r, _ in pairs if r.failure == "validation_failed")
    timeout_count = sum(1 for r, _ in pairs if r.failure == "evaluator_timeout")

    assert pass_count == 30, f"pass_count={pass_count} (expected 30)"
    assert reject_count == 15, f"reject_count={reject_count} (expected 15)"
    assert timeout_count == 5, f"timeout_count={timeout_count} (expected 5)"


# ---------------------------------------------------------------------------
# b. Memory under load
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_memory_under_load_sequential_50_runs() -> None:
    """50 sequential fast-pass runs must not cause excessive RSS growth.

    The informational threshold is 100 MB. RSS is printed for visibility
    even if below threshold (so CI logs show the actual value).
    """
    psutil = pytest.importorskip("psutil")

    sb = PythonEvaluatorSandbox()
    proc = psutil.Process(os.getpid())

    # Warm up
    _run_with_sandbox(sb, "return True")

    rss_before = proc.memory_info().rss

    for _ in range(50):
        result = _run_with_sandbox(sb, "return True")
        assert result.failure is None, f"Unexpected failure: {result.failure}"
        assert result.value is True

    rss_after = proc.memory_info().rss
    delta_mb = (rss_after - rss_before) / (1024 * 1024)

    print(
        f"\n[memory_under_load] RSS before={rss_before / 1024 / 1024:.1f} MB, "
        f"after={rss_after / 1024 / 1024:.1f} MB, delta={delta_mb:.1f} MB"
    )

    assert delta_mb < 100, (
        f"RSS grew by {delta_mb:.1f} MB after 50 sequential runs (threshold: 100 MB)"
    )
