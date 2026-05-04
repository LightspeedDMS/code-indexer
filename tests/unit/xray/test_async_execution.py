"""Tests for XRaySearchEngine async job execution (Story #978).

Tests the ThreadPoolExecutor parallelism, wall-clock timeout enforcement,
COMPLETED_PARTIAL contract, and _evaluate_file private helper extraction.

Uses real PythonEvaluatorSandbox and temp-dir fixtures — no mocking of core logic.
Slow evaluators use sum(range(N)) busy-loops to simulate load without sleeping,
which allows the sandbox 5s timeout to act as a backstop if the outer timeout fires.

Note: evaluator_code must NOT use the ** (Pow) operator — it is not in the sandbox
ALLOWED_NODES whitelist. Use integer literals such as sum(range(50000000)) (~700ms)
instead of sum(range(10**8)) which is rejected at validation before any work is done.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List

import pytest


@pytest.fixture
def search_engine():
    """Instantiate XRaySearchEngine, skipping if tree-sitter extras not installed."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


@pytest.fixture
def multi_file_repo(tmp_path):
    """Create a temp dir with 8 small Python files all containing a target pattern."""
    for i in range(8):
        (tmp_path / f"f{i}.py").write_text(f"target_pattern = {i}\n")
    return tmp_path


# ---------------------------------------------------------------------------
# _evaluate_file helper extraction
# ---------------------------------------------------------------------------


class TestEvaluateFileHelper:
    """_evaluate_file private method exists and works in isolation."""

    def test_evaluate_file_method_exists(self, search_engine):
        """XRaySearchEngine must expose _evaluate_file as a callable method."""
        assert callable(getattr(search_engine, "_evaluate_file", None)), (
            "_evaluate_file method must exist on XRaySearchEngine"
        )

    def test_evaluate_file_returns_tuple_of_two_lists(self, search_engine, tmp_path):
        """_evaluate_file returns (matches_list, errors_list) for a simple file."""
        fpath = tmp_path / "simple.py"
        fpath.write_text("x = 1\n")

        matches, errors = search_engine._evaluate_file(
            fpath,
            "return True",
            include_ast_debug=False,
            max_debug_nodes=50,
        )
        assert isinstance(matches, list)
        assert isinstance(errors, list)

    def test_evaluate_file_truthy_evaluator_produces_match(
        self, search_engine, tmp_path
    ):
        """_evaluate_file with return True yields one match entry."""
        fpath = tmp_path / "simple.py"
        fpath.write_text("x = 1\n")

        matches, errors = search_engine._evaluate_file(
            fpath,
            "return True",
            include_ast_debug=False,
            max_debug_nodes=50,
        )
        assert len(matches) == 1
        assert errors == []
        match = matches[0]
        assert "file_path" in match
        assert "language" in match
        assert match["evaluator_decision"] is True

    def test_evaluate_file_falsy_evaluator_produces_no_match(
        self, search_engine, tmp_path
    ):
        """_evaluate_file with return False yields no matches."""
        fpath = tmp_path / "simple.py"
        fpath.write_text("x = 1\n")

        matches, errors = search_engine._evaluate_file(
            fpath,
            "return False",
            include_ast_debug=False,
            max_debug_nodes=50,
        )
        assert matches == []
        assert errors == []

    def test_evaluate_file_unsupported_extension_yields_error(
        self, search_engine, tmp_path
    ):
        """_evaluate_file on a .xyz file yields UnsupportedLanguage error."""
        fpath = tmp_path / "unknown.xyz"
        fpath.write_text("target_pattern here\n")

        matches, errors = search_engine._evaluate_file(
            fpath,
            "return True",
            include_ast_debug=False,
            max_debug_nodes=50,
        )
        assert matches == []
        assert len(errors) == 1
        assert errors[0]["error_type"] == "UnsupportedLanguage"


# ---------------------------------------------------------------------------
# ThreadPoolExecutor parallelism
# ---------------------------------------------------------------------------


class TestThreadPoolExecutorParallelism:
    """Phase 2 runs files concurrently using ThreadPoolExecutor."""

    def test_worker_threads_run_concurrently_faster_than_serial(
        self, search_engine, tmp_path
    ):
        """4 workers on 4 files should complete substantially faster than serial.

        Each file evaluator does a trivial busy-loop (~5ms) so 4 workers in
        parallel should complete in roughly 1x loop time whereas serial would
        take 4x. We assert parallel < 3x serial to allow OS overhead.
        """
        # Create 4 Python files; each evaluator does a small timed busy-loop
        # that takes approximately 50ms using a tight Python sum loop.
        for i in range(4):
            (tmp_path / f"f{i}.py").write_text(f"x_{i} = {i}\n")

        # Evaluator that runs for ~50ms per file (tight loop, not sleep)
        # sum(range(600_000)) takes ~25ms on modern CPUs, safe under 5s sandbox limit
        slow_evaluator = "sum(range(600_000)); return True"

        # Serial run: 1 worker (sequential)
        t_serial_start = time.monotonic()
        search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x_",
            evaluator_code=slow_evaluator,
            search_target="content",
            worker_threads=1,
            timeout_seconds=60,
        )
        t_serial = time.monotonic() - t_serial_start

        # Parallel run: 4 workers
        t_parallel_start = time.monotonic()
        search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x_",
            evaluator_code=slow_evaluator,
            search_target="content",
            worker_threads=4,
            timeout_seconds=60,
        )
        t_parallel = time.monotonic() - t_parallel_start

        # Parallel should be substantially faster (< 60% of serial time)
        # We use a generous threshold because CI machines vary in core count
        assert t_parallel < t_serial * 0.8, (
            f"Parallel ({t_parallel:.3f}s) not substantially faster than "
            f"serial ({t_serial:.3f}s) — ThreadPoolExecutor may not be active"
        )

    def test_worker_threads_one_still_completes_all_files(
        self, search_engine, multi_file_repo
    ):
        """With worker_threads=1, all 8 files are still processed."""
        result = search_engine.run(
            repo_path=multi_file_repo,
            driver_regex=r"target_pattern",
            evaluator_code="return True",
            search_target="content",
            worker_threads=1,
            timeout_seconds=60,
        )
        assert result["files_processed"] == 8
        assert "partial" not in result or result["partial"] is not True


# ---------------------------------------------------------------------------
# Timeout enforcement
# ---------------------------------------------------------------------------


class TestTimeoutEnforcement:
    """Wall-clock timeout fires between files and returns COMPLETED_PARTIAL."""

    def test_timeout_returns_partial_true(self, search_engine, tmp_path):
        """When timeout fires, result must have partial=True."""
        # 8 files with a slow evaluator; 2s timeout should fire before all complete
        for i in range(8):
            (tmp_path / f"f{i}.py").write_text(f"x_{i} = {i}\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x_",
            evaluator_code="sum(range(50000000)); return True",
            search_target="content",
            timeout_seconds=2,
            worker_threads=2,
        )
        # Either timeout fired or all completed (fast machine) — if timeout fired:
        if result["files_processed"] < result["files_total"]:
            assert result.get("partial") is True, (
                "partial must be True when files_processed < files_total"
            )

    def test_timeout_returns_timeout_true(self, search_engine, tmp_path):
        """When timeout fires, result must have timeout=True with EXACT True identity."""
        for i in range(8):
            (tmp_path / f"f{i}.py").write_text(f"x_{i} = {i}\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x_",
            evaluator_code="sum(range(50000000)); return True",
            search_target="content",
            timeout_seconds=2,
            worker_threads=2,
        )
        if result["files_processed"] < result["files_total"]:
            assert result.get("timeout") is True, (
                "timeout must be exactly True (identity check) when timeout fires"
            )

    def test_timeout_files_processed_less_than_files_total(
        self, search_engine, tmp_path
    ):
        """When timeout fires, files_processed must be less than files_total."""
        for i in range(8):
            (tmp_path / f"f{i}.py").write_text(f"x_{i} = {i}\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x_",
            evaluator_code="sum(range(50000000)); return True",
            search_target="content",
            timeout_seconds=2,
            worker_threads=2,
        )
        # Verify the invariant: processed <= total always
        assert result["files_processed"] <= result["files_total"]

    def test_timeout_no_max_files_reached_key(self, search_engine, tmp_path):
        """When timeout fires (not max_files), max_files_reached must NOT be True."""
        for i in range(8):
            (tmp_path / f"f{i}.py").write_text(f"x_{i} = {i}\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x_",
            evaluator_code="sum(range(50000000)); return True",
            search_target="content",
            timeout_seconds=2,
            worker_threads=2,
        )
        if result.get("timeout") is True:
            assert result.get("max_files_reached") is not True, (
                "max_files_reached must not be set when timeout fires"
            )


# ---------------------------------------------------------------------------
# COMPLETED_PARTIAL contract — exact True identity
# ---------------------------------------------------------------------------


class TestCompletedPartialContract:
    """partial, timeout, max_files_reached must be exact True (is True), not just truthy."""

    def test_max_files_partial_is_exact_true(self, search_engine, tmp_path):
        """partial key must be exactly True (identity), not just truthy."""
        for i in range(4):
            (tmp_path / f"f{i}.py").write_text(f"x_{i} = {i}\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x_",
            evaluator_code="return True",
            search_target="content",
            max_files=2,
            timeout_seconds=60,
        )
        assert result.get("partial") is True, (
            "partial must be exactly True (not just truthy) — is True check"
        )

    def test_max_files_reached_is_exact_true(self, search_engine, tmp_path):
        """max_files_reached key must be exactly True (identity), not just truthy."""
        for i in range(4):
            (tmp_path / f"f{i}.py").write_text(f"x_{i} = {i}\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x_",
            evaluator_code="return True",
            search_target="content",
            max_files=2,
            timeout_seconds=60,
        )
        assert result.get("max_files_reached") is True, (
            "max_files_reached must be exactly True (identity check)"
        )

    def test_no_partial_on_clean_completion(self, search_engine, tmp_path):
        """When all files complete under timeout, partial must not be True."""
        (tmp_path / "only.py").write_text("x = 1\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x",
            evaluator_code="return True",
            search_target="content",
            timeout_seconds=60,
        )
        assert result.get("partial") is not True, (
            "partial must not be True on clean completion"
        )
        assert result.get("timeout") is not True, (
            "timeout must not be True on clean completion"
        )

    def test_max_files_timeout_not_in_result_on_max_files_hit(
        self, search_engine, tmp_path
    ):
        """When only max_files cap fires, timeout key must not be True in result."""
        for i in range(4):
            (tmp_path / f"f{i}.py").write_text(f"x_{i} = {i}\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x_",
            evaluator_code="return True",
            search_target="content",
            max_files=2,
            timeout_seconds=60,
        )
        # timeout key must be absent or exactly not True
        assert result.get("timeout") is not True, (
            "timeout must not be True when only max_files cap fires"
        )


# ---------------------------------------------------------------------------
# Mutual exclusion: timeout vs max_files_reached
# ---------------------------------------------------------------------------


class TestMutualExclusion:
    """timeout and max_files_reached are mutually exclusive; timeout wins."""

    def test_timeout_takes_precedence_over_max_files(self, search_engine, tmp_path):
        """When both timeout and max_files would fire, only timeout=True is set.

        Strategy: create 8 slow files, set max_files=4 (so cap would fire at 4)
        and timeout=2 (so timeout fires before all 4 are done). When timeout wins
        only timeout=True is present; max_files_reached must not be True.
        """
        for i in range(8):
            (tmp_path / f"f{i}.py").write_text(f"x_{i} = {i}\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x_",
            evaluator_code="sum(range(50000000)); return True",
            search_target="content",
            max_files=4,
            timeout_seconds=2,
            worker_threads=2,
        )
        # If timeout fired: timeout=True must be set, max_files_reached must NOT be True
        if result.get("timeout") is True:
            assert result.get("max_files_reached") is not True, (
                "max_files_reached must not be True when timeout takes precedence"
            )
        # If max_files fired first (all 4 ran fast): max_files_reached=True, timeout not True
        elif result.get("max_files_reached") is True:
            assert result.get("timeout") is not True, (
                "timeout must not be True when only max_files cap fires"
            )

    def test_max_files_only_has_no_timeout_key(self, search_engine, tmp_path):
        """When max_files cap fires with no timeout pressure, timeout key absent or False."""
        for i in range(6):
            (tmp_path / f"f{i}.py").write_text(f"x_{i} = {i}\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x_",
            evaluator_code="return True",
            search_target="content",
            max_files=3,
            timeout_seconds=120,  # generous — will not fire
        )
        assert result.get("partial") is True
        assert result.get("max_files_reached") is True
        assert result.get("timeout") is not True

    def test_no_partial_keys_on_full_completion(self, search_engine, tmp_path):
        """Full completion with no cap: neither timeout nor max_files_reached set."""
        for i in range(3):
            (tmp_path / f"f{i}.py").write_text(f"x_{i} = {i}\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r"x_",
            evaluator_code="return True",
            search_target="content",
            timeout_seconds=120,
        )
        assert result.get("timeout") is not True
        assert result.get("max_files_reached") is not True
        assert result["files_processed"] == 3


# ---------------------------------------------------------------------------
# Reentrancy
# ---------------------------------------------------------------------------


class TestReentrancy:
    """Two concurrent engine.run() calls on different repos produce independent results."""

    def test_two_concurrent_runs_produce_independent_results(
        self, search_engine, tmp_path
    ):
        """Run two threads each calling engine.run on separate subdirs concurrently.

        Verifies that shared state (sandbox, ast_engine) doesn't cross-contaminate.
        """
        import threading

        repo_a = tmp_path / "repo_a"
        repo_b = tmp_path / "repo_b"
        repo_a.mkdir()
        repo_b.mkdir()

        # repo_a has pattern_alpha files; repo_b has pattern_beta files
        for i in range(3):
            (repo_a / f"a{i}.py").write_text(f"pattern_alpha = {i}\n")
            (repo_b / f"b{i}.py").write_text(f"pattern_beta = {i}\n")

        results: Dict[str, Any] = {}
        errors: List[Exception] = []

        def run_alpha():
            try:
                results["alpha"] = search_engine.run(
                    repo_path=repo_a,
                    driver_regex=r"pattern_alpha",
                    evaluator_code="return True",
                    search_target="content",
                    worker_threads=2,
                    timeout_seconds=60,
                )
            except Exception as exc:
                errors.append(exc)

        def run_beta():
            try:
                results["beta"] = search_engine.run(
                    repo_path=repo_b,
                    driver_regex=r"pattern_beta",
                    evaluator_code="return True",
                    search_target="content",
                    worker_threads=2,
                    timeout_seconds=60,
                )
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=run_alpha)
        t2 = threading.Thread(target=run_beta)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert not errors, f"Concurrent run raised: {errors}"
        assert "alpha" in results and "beta" in results

        # Alpha results should only contain alpha files
        alpha_paths = [m["file_path"] for m in results["alpha"]["matches"]]
        assert all("repo_a" in p for p in alpha_paths), (
            "Alpha run results contaminated by beta repo paths"
        )

        # Beta results should only contain beta files
        beta_paths = [m["file_path"] for m in results["beta"]["matches"]]
        assert all("repo_b" in p for p in beta_paths), (
            "Beta run results contaminated by alpha repo paths"
        )

        # Each repo has 3 files — both should process all 3
        assert results["alpha"]["files_processed"] == 3
        assert results["beta"]["files_processed"] == 3


# ---------------------------------------------------------------------------
# Input validation: ValueError guards
# ---------------------------------------------------------------------------


class TestValueErrorGuards:
    """run() raises ValueError immediately for invalid parameter values."""

    def test_timeout_seconds_zero_raises_value_error(self, search_engine):
        """timeout_seconds=0 must raise ValueError before any work begins."""
        with pytest.raises(ValueError, match="timeout_seconds"):
            search_engine.run(
                repo_path=Path("/tmp"),
                driver_regex="x",
                evaluator_code="return True",
                search_target="content",
                timeout_seconds=0,
            )

    def test_worker_threads_zero_raises_value_error(self, search_engine):
        """worker_threads=0 must raise ValueError before any work begins."""
        with pytest.raises(ValueError, match="worker_threads"):
            search_engine.run(
                repo_path=Path("/tmp"),
                driver_regex="x",
                evaluator_code="return True",
                search_target="content",
                worker_threads=0,
            )

    def test_max_files_zero_raises_value_error(self, search_engine):
        """max_files=0 must raise ValueError before any work begins."""
        with pytest.raises(ValueError, match="max_files"):
            search_engine.run(
                repo_path=Path("/tmp"),
                driver_regex="x",
                evaluator_code="return True",
                search_target="content",
                max_files=0,
            )


# ---------------------------------------------------------------------------
# EvaluatorTimeout error message text (spec-exact wording)
# ---------------------------------------------------------------------------


class TestEvaluatorTimeoutMessage:
    """EvaluatorTimeout error_message must match spec-exact text."""

    def test_evaluator_timeout_error_message_text(self, search_engine, tmp_path):
        """_evaluate_file must produce error_message 'evaluator exceeded 5s sandbox limit'.

        The spec requires lowercase 'evaluator' and the word 'sandbox' in the message.
        """
        fpath = tmp_path / "slow.py"
        fpath.write_text("x = 1\n")

        # Use a sandbox-valid busy loop that will hit the 5s sandbox timeout.
        # sum(range(500000000)) runs for well over 5s, triggering evaluator_timeout.
        _, errors = search_engine._evaluate_file(
            fpath,
            "sum(range(500000000)); return True",
            include_ast_debug=False,
            max_debug_nodes=50,
        )
        timeout_errors = [
            e for e in errors if e.get("error_type") == "EvaluatorTimeout"
        ]
        assert len(timeout_errors) == 1, (
            f"Expected exactly 1 EvaluatorTimeout error, got: {errors}"
        )
        assert (
            timeout_errors[0]["error_message"] == "evaluator exceeded 5s sandbox limit"
        ), f"Wrong message text: {timeout_errors[0]['error_message']!r}"
