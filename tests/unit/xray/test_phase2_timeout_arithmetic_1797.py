"""Bug #1797: Phase-2 timeout discards the specific error and can overrun
timeout_seconds by ~1s.

Two independent defects in ``XRaySearchEngine.run()``'s Phase 2 batch loop:

1. The loop ``break``s on timeout BEFORE extending ``evaluation_errors`` with
   the current batch item's ``file_errors``, so the precise
   ``XRayCliError: "xray-cli timed out after Ns"`` produced by
   ``RustNativeBackend.run_batch`` (rust_backend.py:827) is discarded and
   replaced by a generic synthetic ``EvaluatorTimeout``. Both must survive:
   the synthetic error is the caller-facing classification, the specific
   error is the diagnostic detail (epic #1786 / story #1787 AC7 requires
   every injected failure to produce a distinct, inspectable status).

2. ``remaining = max(1, timeout_seconds - int(_elapsed()))`` floors the
   elapsed float BEFORE subtracting, which can hand ``run_batch`` up to
   ~1s MORE budget than truly remains -- so total wall-clock can exceed
   ``timeout_seconds`` by up to ~1s.

Mocking posture: defect 1's test patches ``time.monotonic`` using the exact
scaffold already proven in
``test_search_engine.py::test_timeout_with_no_results_adds_synthetic_evaluator_timeout_error``
(a real, working pattern in this codebase) to deterministically force the
job-level timeout without depending on real xray-cli subprocess timing.
Defect 2's test uses REAL wall-clock time (no clock mocking, and nothing on
``XRaySearchEngine`` itself is patched): the only two things replaced are
external collaborators -- ``ast_engine.detect_language`` (padded with a
real, controlled ``time.sleep`` so real elapsed time is genuinely spent
before Phase 2's `remaining` is computed) and ``rust_backend.run_batch``
(a real-sleeping fake, mirroring ``test_async_execution.py``'s
``_slow_mock_run_batch`` pattern) -- so the reported overrun is genuinely
observed wall-clock time driven through the real, unpatched ``run()``
arithmetic.
"""

from __future__ import annotations

import time as _time
from typing import Any, Callable, Dict, List, Tuple
from unittest.mock import patch

import pytest


@pytest.fixture
def search_engine():
    """Instantiate a real XRaySearchEngine, skipping if extras are absent."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


class TestSpecificTimeoutErrorSurvivesAlongsideSynthetic:
    """Defect 1: the specific XRayCliError must survive into evaluation_errors
    alongside the synthetic EvaluatorTimeout classification -- neither one
    replaces the other."""

    def test_specific_xray_cli_error_preserved_alongside_synthetic_timeout(
        self, search_engine, tmp_path
    ):
        (tmp_path / "file.py").write_text("prepareStatement()\n")

        # Exact contract RustNativeBackend.run_batch returns for a whole-batch
        # (aggregate) failure such as a real xray-cli subprocess timeout --
        # see rust_backend.py:569's `_error_tuple("", "XRayCliError", ...)`
        # and its literal message text at rust_backend.py:827.
        specific_message = "xray-cli timed out after 5s"
        timeout_batch_result: List[Tuple[List[Any], List[Dict[str, Any]], None]] = [
            (
                [],
                [
                    {
                        "file_path": "",
                        "line_number": 0,
                        "error_type": "XRayCliError",
                        "error_message": specific_message,
                    }
                ],
                None,
            )
        ]

        # Same fake-monotonic scaffold as
        # test_search_engine.py::test_timeout_with_no_results_adds_synthetic_evaluator_timeout_error
        # (already proven not to trip Phase 1's own timeout check for this
        # exact repo/regex/search_target combination): first call returns the
        # real start value (used by `start = time.monotonic()`), every call
        # after that returns a value far past the deadline so `_timed_out()`
        # is True for the whole Phase 2 loop.
        monotonic_calls: List[int] = []
        start_value = _time.monotonic()

        def _fake_monotonic() -> float:
            monotonic_calls.append(1)
            if len(monotonic_calls) == 1:
                return start_value
            return start_value + 200.0

        with patch.object(
            search_engine.rust_backend,
            "run_batch",
            return_value=timeout_batch_result,
        ):
            with patch(
                "code_indexer.xray.search_engine.time.monotonic", _fake_monotonic
            ):
                result = search_engine.run(
                    repo_path=tmp_path,
                    driver_regex=r"prepareStatement",
                    evaluator_code="Vec::new()",
                    search_target="content",
                    timeout_seconds=1,
                )

        assert result.get("timeout") is True, "result must have timeout=True"
        assert result.get("partial") is True, "result must have partial=True"

        errors = result.get("evaluation_errors", [])
        error_types = [e["error_type"] for e in errors]

        # RED on current code: the loop `break`s before extending
        # evaluation_errors with this batch item's file_errors, so the
        # specific XRayCliError never survives.
        assert "XRayCliError" in error_types, (
            "The specific XRayCliError from run_batch must survive into "
            f"evaluation_errors. Got error_types={error_types!r}"
        )
        specific_errors = [e for e in errors if e["error_type"] == "XRayCliError"]
        assert specific_errors[0]["error_message"] == specific_message, (
            f"Expected preserved message {specific_message!r}, "
            f"got {specific_errors[0]['error_message']!r}"
        )

        # The synthetic caller-facing classification must ALSO still be
        # present -- one must not replace the other (bug #1797 fix
        # direction #1: "preserve the original error alongside the
        # synthetic one").
        assert "EvaluatorTimeout" in error_types, (
            "The synthetic EvaluatorTimeout classification must still be "
            f"present alongside the specific error. Got: {error_types!r}"
        )


# ---------------------------------------------------------------------------
# Defect 2 helpers -- kept as small module-level factories (rather than
# inline closures) so the test method itself stays short and readable.
# ---------------------------------------------------------------------------


def _make_delayed_detect_language(
    real_detect_language: Callable[..., Any], delay_seconds: float
) -> Callable[..., Any]:
    """Wrap the REAL ``detect_language`` collaborator with a real, controlled
    ``time.sleep`` pad, so real wall-clock time is genuinely spent before
    Phase 2's ``remaining`` is computed -- the return value is untouched
    (still the real detected language)."""

    def _delayed(*args: Any, **kwargs: Any) -> Any:
        result = real_detect_language(*args, **kwargs)
        _time.sleep(delay_seconds)
        return result

    return _delayed


def _make_recording_sleepy_run_batch(
    captured_remaining: List[int],
) -> Callable[..., Any]:
    """Fake ``rust_backend.run_batch`` that really sleeps for whatever
    ``timeout_seconds`` (i.e. the engine's computed ``remaining``) it is
    called with, then returns the same aggregate-timeout tuple shape
    ``RustNativeBackend.run_batch`` produces on a real xray-cli subprocess
    timeout (rust_backend.py:569/827)."""

    def _run_batch(**kwargs: Any) -> Any:
        remaining = kwargs["timeout_seconds"]
        captured_remaining.append(remaining)
        _time.sleep(remaining)
        return [
            (
                [],
                [
                    {
                        "file_path": "",
                        "line_number": 0,
                        "error_type": "XRayCliError",
                        "error_message": f"xray-cli timed out after {remaining}s",
                    }
                ],
                None,
            )
        ]

    return _run_batch


class TestTimeoutArithmeticDoesNotOverrun:
    """Defect 2: total wall-clock must not exceed timeout_seconds by more
    than normal scheduling jitter -- the truncating
    `timeout_seconds - int(_elapsed())` formula can overrun by up to ~1s."""

    # Arithmetic with PHASE1_REAL_DELAY_SECONDS=1.9, TIMEOUT_SECONDS=3:
    #   - Buggy `max(1, timeout_seconds - int(_elapsed()))`:
    #     int(~1.9) == 1  ->  remaining = max(1, 3-1) = 2
    #     total ~= 1.9 (real delay) + 2 (fake run_batch sleep) = 3.9s
    #     (~0.9s over budget).
    #   - Fixed `max(1, int(timeout_seconds - _elapsed()))`:
    #     int(3 - ~1.9) == int(~1.1) == 1  ->  remaining = max(1, 1) = 1
    #     total ~= 1.9 + 1 = 2.9s (under budget).
    #
    # Tolerance: OVERRUN_TOLERANCE_SECONDS=0.4s. The buggy formula's ~0.9s
    # overrun clears this tolerance by more than 2x, while normal
    # scheduling/GIL/subprocess-teardown jitter on this fast, tiny-repo path
    # is on the order of tens of milliseconds -- nowhere near 0.4s -- so the
    # tolerance discriminates the real bug without being flaky. int(_elapsed())
    # stays pinned at 1 for any real elapsed in [1.0, 2.0) -- a full second
    # of slack around the chosen 1.9s delay -- so the buggy-vs-fixed
    # remaining values (2 vs 1) are robust to normal timing jitter, not just
    # the exact 1.9s target.
    PHASE1_REAL_DELAY_SECONDS = 1.9
    TIMEOUT_SECONDS = 3
    OVERRUN_TOLERANCE_SECONDS = 0.4

    def test_real_timeout_does_not_overrun_by_more_than_jitter_tolerance(
        self, search_engine, tmp_path
    ):
        """Drive a REAL timeout (no clock mocking, nothing on the SUT class
        itself is patched) and measure real wall-clock elapsed against
        timeout_seconds. See class-level comment for the arithmetic and
        tolerance rationale."""
        (tmp_path / "file.py").write_text("prepareStatement()\n")

        captured_remaining: List[int] = []
        real_detect_language = search_engine.ast_engine.detect_language

        with patch.object(
            search_engine.ast_engine,
            "detect_language",
            side_effect=_make_delayed_detect_language(
                real_detect_language, self.PHASE1_REAL_DELAY_SECONDS
            ),
        ):
            with patch.object(
                search_engine.rust_backend,
                "run_batch",
                side_effect=_make_recording_sleepy_run_batch(captured_remaining),
            ):
                wall_clock_start = _time.monotonic()
                result = search_engine.run(
                    repo_path=tmp_path,
                    driver_regex=r"prepareStatement",
                    evaluator_code="Vec::new()",
                    search_target="content",
                    timeout_seconds=self.TIMEOUT_SECONDS,
                )
                wall_clock_elapsed = _time.monotonic() - wall_clock_start

        assert captured_remaining, "run_batch was never invoked"
        # `timeout_hit` fires because run_batch consumed its entire granted
        # sub-budget (the search_engine's own exhausted-sub-budget
        # detection), not necessarily because the OUTER wall-clock crossed
        # TIMEOUT_SECONDS -- a correctly-bounded `remaining` can keep total
        # elapsed under budget even when Phase 2 itself genuinely timed out.
        assert result.get("timeout") is True, "expected a Phase 2 timeout result"
        error_types = [e["error_type"] for e in result.get("evaluation_errors", [])]
        assert "XRayCliError" in error_types, (
            f"Expected the fake run_batch's timeout error to surface, got {error_types!r}"
        )

        # RED on current code: buggy remaining=2 pushes real elapsed to
        # ~3.9s, which exceeds TIMEOUT_SECONDS + OVERRUN_TOLERANCE_SECONDS
        # (3.4s).
        limit = self.TIMEOUT_SECONDS + self.OVERRUN_TOLERANCE_SECONDS
        assert wall_clock_elapsed <= limit, (
            f"Total wall-clock ({wall_clock_elapsed:.3f}s) exceeded "
            f"timeout_seconds ({self.TIMEOUT_SECONDS}s) by more than the "
            f"{self.OVERRUN_TOLERANCE_SECONDS}s jitter tolerance. "
            f"captured remaining={captured_remaining!r}"
        )
