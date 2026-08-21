"""Bug #1598: xray_search timeout_seconds not enforced during Phase 1
(candidate-file selection).

Phase 1 (regex/filename driver) never received the caller's timeout_seconds
budget, so a request with a short deadline could still run Phase 1 for an
unbounded amount of time. This file exercises both Phase 1 implementations:

- search_target="filename": inline path-walker (XRaySearchEngine._run_phase1_filename)
  must check elapsed time DURING the rglob() traversal itself (not merely
  after it, and not merely between per-file evaluations) and raise a
  dedicated XRayPhase1TimeoutError carrying partial candidates collected so
  far.
- search_target="content": delegates to RegexSearchService.search(), which
  already accepts/enforces timeout_seconds -- the fix is to forward the
  parameter, and to also forward it into the zero-match-pattern probe
  (_probe_zero_match_patterns_content), and to catch the resulting
  TimeoutError and translate it into the same XRayPhase1TimeoutError so a
  phase-1 timeout never escapes XRaySearchEngine.run() as a raw exception.

No mocking of the rglob() walk itself for the filename-mode timing tests:
a real, on-disk directory tree is created and Path.is_file is monkeypatched
to add a small delay -- this simulates a slow filesystem syscall exactly at
the point the real code already calls it per-entry, so the timeout genuinely
fires mid-traversal.

AC coverage map:
  AC-1  TestFilenameModeTimeout.test_timeout_during_traversal_reports_partial
  AC-2  TestContentModeTimeout (both tests)
  AC-3  TestFilenameModeTimeout.test_happy_path_* (both tests)
  AC-4  TestFilenameModeTimeout.test_timeout_burns_zero_phase2_budget
  AC-5  TestContentModeTimeout.test_timeout_does_not_crash_run +
        TestFilenameModeTimeout.test_timeout_during_traversal_reports_partial
        (both assert the graceful completed/timeout shape, never an
        uncaught exception)
  AC-6  TestFilenameModeTimeout.test_timeout_during_traversal_reports_partial
  AC-7  TestContentModeTimeoutForwarding.test_forwards_timeout_seconds_to_zero_match_probe
  AC-8  NOT re-tested here (out of scope for this file by design): this fix
        touches only Phase 1 candidate-selection code and never the
        SIGTERM/SIGKILL cancel_job path. The pre-existing regression
        coverage for that path lives in
        tests/unit/server/mcp/test_xray_cancel_bug1070.py, which is run
        unmodified as part of this bug's verification to confirm
        cancellation still works exactly as before.
  AC-9  TestFilenameModeTimeout.test_xray_explore_ast_debug_mode_also_honors_timeout
  AC-10 all of the above use real on-disk trees + a slowed real syscall for
        the filename-mode walk (never a mocked rglob), and mock only the
        RegexSearchService.search() boundary (never internal walk logic)
        for content-mode.
  AC-11 verified by running the pre-existing
        tests/unit/xray/test_search_engine.py,
        tests/unit/xray/test_phase1_driver_regex_service.py,
        tests/unit/xray/test_zero_match_probe_read_capped_1601.py,
        tests/unit/xray/test_phase1_driver_read_capped_1601.py,
        tests/unit/xray/test_code_indexer_dir_exclusion_v10_4_4.py,
        tests/unit/xray/test_git_dir_exclusion_v10_4_6.py, and
        tests/unit/server/mcp/test_xray_pcre2_invalid_regex_v10_4_4.py
        files unmodified alongside this one -- not duplicated here.
  AC-12 _search_python_multiline (src/code_indexer/global_repos/regex_search.py)
        still has no timeout_seconds parameter as of this fix and remains
        explicitly, deliberately out of scope: it is the pure-Python
        multiline fallback used only when multiline=True on the grep engine
        or when ripgrep is unavailable, neither of which this bug's fix
        touches. Filed as priority-4 GitHub follow-up issue #1611 rather
        than left as an undocumented residual gap.

Review-finding coverage (post-merge review round):
  R1  TestZeroMatchProbeSharedBudget.test_probe_shares_one_budget_across_patterns
      -- the zero-match-pattern probe loop previously passed the caller's
      FULL timeout_seconds to EVERY one of N include_patterns' search()
      calls, so N patterns could burn N * timeout_seconds. Fixed to share
      ONE remaining budget (main search elapsed time subtracted) across the
      whole probe loop, with the loop itself stopping early once that
      shared budget is exhausted.
  R2  TestZeroMatchProbeTimeoutPreservesMainSearch.test_probe_timeout_preserves_main_search_candidates
      -- a probe timeout previously raised XRayPhase1TimeoutError, which
      discarded candidates the MAIN search had already found successfully
      within budget. Fixed so a probe timeout is advisory-only: it is
      recorded as a warning and the function returns the main search's
      real candidates normally.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_DEFAULT_EVALUATOR = (
    'matches = [{"line_number": mp["line_number"]} for mp in match_positions]\n'
    'return {"matches": matches, "value": None}'
)


def _make_regex_match(file_path: str, line_number: int = 1, line_content: str = "x"):
    from code_indexer.global_repos.regex_search import RegexMatch

    return RegexMatch(
        file_path=file_path,
        line_number=line_number,
        column=1,
        line_content=line_content,
    )


def _make_search_result(matches, read_capped: bool = False):
    from code_indexer.global_repos.regex_search import RegexSearchResult

    return RegexSearchResult(
        matches=matches,
        total_matches=len(matches),
        truncated=False,
        search_engine="ripgrep",
        search_time_ms=0.0,
        read_capped=read_capped,
    )


@pytest.fixture
def search_engine():
    """Instantiate XRaySearchEngine, skipping if tree-sitter extras not installed."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


def _build_slow_tree(tmp_path: Path, total_files: int = 100, num_dirs: int = 5) -> None:
    """Create a real, on-disk directory tree with total_files real files."""
    for i in range(total_files):
        d = tmp_path / f"pkg{i % num_dirs}"
        d.mkdir(exist_ok=True)
        (d / f"mod{i}.py").write_text(f"# file {i}\n")


def _install_slow_is_file(monkeypatch: pytest.MonkeyPatch, delay: float) -> None:
    """Monkeypatch Path.is_file to sleep `delay` seconds before delegating.

    This slows down the exact per-entry syscall the real Phase 1 filename
    walker already calls once per rglob() entry -- it does NOT mock rglob()
    itself, and the delay fires interleaved with the traversal (not after
    the whole tree has already been collected).
    """
    real_is_file = Path.is_file

    def _slow_is_file(self_path: Path, *args: Any, **kwargs: Any) -> bool:
        time.sleep(delay)
        return real_is_file(self_path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", _slow_is_file)


# ---------------------------------------------------------------------------
# AC-1, AC-6, AC-10: filename-mode timeout fires DURING the traversal and
# preserves partial candidates.
# ---------------------------------------------------------------------------


class TestFilenameModeTimeout:
    def test_timeout_during_traversal_reports_partial(
        self, search_engine, tmp_path, monkeypatch
    ):
        # 150 files + dirs at 0.03s/entry ~= 4.65s full-walk time -- well
        # clear of the 3s tolerance ceiling below, so this genuinely
        # discriminates the buggy (unbounded walk) code from the fix
        # (bounded near timeout_seconds=1) rather than relying on a tight
        # timing margin that could pass by coincidence either way.
        total_files = 150
        _build_slow_tree(tmp_path, total_files=total_files)
        _install_slow_is_file(monkeypatch, delay=0.03)

        start = time.monotonic()
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r".*",
            evaluator_code=_DEFAULT_EVALUATOR,
            search_target="filename",
            timeout_seconds=1,
        )
        wall_elapsed = time.monotonic() - start

        # AC-1: terminal status within timeout_seconds + small tolerance (<=2s),
        # not the full ~4.65s unbounded walk duration.
        assert wall_elapsed <= 1 + 2, f"took {wall_elapsed}s, expected <= 3s"

        # AC-5: graceful degradation shape.
        assert result["partial"] is True
        assert result["timeout"] is True
        assert result["files_processed"] == 0
        assert result["matches"] == []

        # AC-6: partial candidates preserved -- non-empty, but did not
        # finish the full walk (proves the timeout fired mid-traversal,
        # not merely after sorted(rglob()) had already materialized
        # everything).
        assert 0 < result["files_total"] < total_files, (
            f"files_total={result['files_total']} total_files={total_files}"
        )

    def test_timeout_burns_zero_phase2_budget(
        self, search_engine, tmp_path, monkeypatch
    ):
        """AC-4: phase 1 timeout must never reach rust_backend.run_batch."""
        _build_slow_tree(tmp_path, total_files=60)
        _install_slow_is_file(monkeypatch, delay=0.03)

        mock_run_batch = MagicMock()
        monkeypatch.setattr(search_engine.rust_backend, "run_batch", mock_run_batch)

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r".*",
            evaluator_code=_DEFAULT_EVALUATOR,
            search_target="filename",
            timeout_seconds=1,
        )

        assert result["timeout"] is True
        mock_run_batch.assert_not_called()

    def test_xray_explore_ast_debug_mode_also_honors_timeout(
        self, search_engine, tmp_path, monkeypatch
    ):
        """AC-9: xray_explore (include_ast_debug=True) shares run() so it
        must inherit the same phase-1 timeout behaviour.

        Uses the same 150-file / 4.65s-full-walk setup as
        test_timeout_during_traversal_reports_partial plus an explicit
        wall-clock assertion: without it, this test can pass by
        coincidence even on unfixed code, because the pre-existing
        phase-2 ``_timed_out()`` check (driven by the SAME timeout_seconds
        value) fires once phase 1 finally returns and happens to produce
        the same partial/timeout result shape -- after the full unbounded
        walk, not within the requested deadline. The wall-clock assertion
        is what actually discriminates the fix from the bug here.
        """
        total_files = 150
        _build_slow_tree(tmp_path, total_files=total_files)
        _install_slow_is_file(monkeypatch, delay=0.03)

        start = time.monotonic()
        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r".*",
            evaluator_code=_DEFAULT_EVALUATOR,
            search_target="filename",
            timeout_seconds=1,
            include_ast_debug=True,
        )
        wall_elapsed = time.monotonic() - start

        assert wall_elapsed <= 1 + 2, f"took {wall_elapsed}s, expected <= 3s"
        assert result["timeout"] is True
        assert result["partial"] is True

    def test_happy_path_preserves_sorted_output_order(self, search_engine, tmp_path):
        """AC-3: when phase 1 finishes comfortably inside the deadline, the
        candidate ordering is unchanged (deterministic, sorted) even though
        the underlying walk is no longer pre-sorted internally."""
        names = ["zeta.py", "alpha.py", "mu.py", "beta.py", "eta.py"]
        for n in names:
            (tmp_path / n).write_text("# x\n")

        candidates = search_engine._run_phase1_driver(
            tmp_path, r".*\.py$", "filename", [], []
        )
        rels = [str(p.relative_to(tmp_path)) for p in candidates]
        assert rels == sorted(rels)
        assert set(rels) == set(names)

    def test_happy_path_no_new_partial_or_timeout_flags(self, search_engine, tmp_path):
        """AC-3: no regression -- comfortable timeout produces no partial/timeout keys."""
        (tmp_path / "a.py").write_text("password = 1\n")

        result = search_engine.run(
            repo_path=tmp_path,
            driver_regex=r".*",
            evaluator_code=_DEFAULT_EVALUATOR,
            search_target="filename",
            timeout_seconds=120,
        )
        assert "timeout" not in result
        assert "partial" not in result


# ---------------------------------------------------------------------------
# AC-2, AC-5: content-mode timeout reports empty partial (asymmetric with
# filename mode, by design -- ripgrep's TimeoutError carries no candidates).
# ---------------------------------------------------------------------------


class TestContentModeTimeout:
    def test_timeout_reports_empty_partial(self, search_engine, tmp_path):
        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(side_effect=TimeoutError("ripgrep timed out"))

            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"pattern",
                evaluator_code=_DEFAULT_EVALUATOR,
                search_target="content",
                timeout_seconds=5,
            )

        assert result["partial"] is True
        assert result["timeout"] is True
        assert result["files_total"] == 0
        assert result["files_processed"] == 0
        assert result["matches"] == []
        assert result["evaluation_errors"] == []

    def test_timeout_does_not_crash_run(self, search_engine, tmp_path):
        """AC-5: a raw TimeoutError from RegexSearchService.search must never
        propagate out of XRaySearchEngine.run() as an unhandled exception."""
        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(side_effect=TimeoutError("ripgrep timed out"))

            # Must not raise.
            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex=r"pattern",
                evaluator_code=_DEFAULT_EVALUATOR,
                search_target="content",
                timeout_seconds=5,
            )
        assert result["timeout"] is True


# ---------------------------------------------------------------------------
# AC-2, AC-10: content-mode correctly forwards timeout_seconds into
# RegexSearchService.search (assert the actual kwarg value).
# ---------------------------------------------------------------------------


class TestContentModeTimeoutForwarding:
    def test_forwards_timeout_seconds_to_main_search(self, search_engine, tmp_path):
        fake_result = _make_search_result([])
        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(return_value=fake_result)

            search_engine._run_phase1_driver(
                tmp_path, "pattern", "content", [], [], timeout_seconds=17
            )

        instance.search.assert_called_once()
        assert instance.search.call_args.kwargs.get("timeout_seconds") == 17

    def test_forwards_timeout_seconds_to_zero_match_probe(
        self, search_engine, tmp_path
    ):
        """AC-7: the zero-match-pattern probe must also receive timeout_seconds."""
        (tmp_path / "a.py").write_text("password = 1\n")
        fake_match = _make_regex_match("a.py")
        fake_result = _make_search_result([fake_match])

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(return_value=fake_result)

            search_engine._run_phase1_driver(
                tmp_path,
                "password",
                "content",
                ["**/*.py"],
                [],
                timeout_seconds=42,
            )

        calls = instance.search.call_args_list
        # First call is the main content search, second is the zero-match
        # probe (both instantiated as RegexSearchService(repo_path), which
        # is the same mock instance).
        assert len(calls) == 2, f"expected 2 search() calls, got {len(calls)}"
        for call in calls:
            assert call.kwargs.get("timeout_seconds") == 42, (
                f"call missing timeout_seconds=42: {call}"
            )


# ---------------------------------------------------------------------------
# R1 (post-merge review finding): the zero-match-pattern probe loop must
# share ONE total wall-clock budget across ALL include_patterns, not give
# each pattern a fresh copy of timeout_seconds.
# ---------------------------------------------------------------------------


class TestZeroMatchProbeSharedBudget:
    def test_probe_shares_one_budget_across_patterns(self, search_engine, tmp_path):
        """With the pre-fix code, N include_patterns each got a fresh
        timeout_seconds budget, so N slow probe calls could burn roughly
        N * timeout_seconds of wall-clock time. This test stubs each probe
        search() call to take a fixed PROBE_DELAY and uses enough patterns
        that the OLD (unfixed) behavior would clearly run for
        NUM_PATTERNS * PROBE_DELAY (well above the assertion threshold
        below), while the fix must keep total wall-clock time close to
        timeout_seconds by stopping the loop once the shared budget runs
        out.
        """
        (tmp_path / "a.py").write_text("password = 1\n")
        fake_match = _make_regex_match("a.py")
        main_result = _make_search_result([fake_match])
        # Zero matches -> would append a zero_match_include_pattern warning
        # per probed pattern; irrelevant to this test's assertions.
        probe_result = _make_search_result([])

        PROBE_DELAY = 0.5
        NUM_PATTERNS = 8
        TIMEOUT_SECONDS = 1

        call_count = {"n": 0}

        async def _search_side_effect(*args: Any, **kwargs: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call is always the main content search.
                return main_result
            # Every subsequent call is one zero-match-pattern probe.
            await asyncio.sleep(PROBE_DELAY)
            return probe_result

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(side_effect=_search_side_effect)

            start = time.monotonic()
            candidates = search_engine._run_phase1_driver(
                tmp_path,
                "password",
                "content",
                [f"**/pat{i}.py" for i in range(NUM_PATTERNS)],
                [],
                timeout_seconds=TIMEOUT_SECONDS,
            )
            wall_elapsed = time.monotonic() - start

        # OLD behavior would take ~NUM_PATTERNS * PROBE_DELAY = 4.0s (each
        # pattern getting its own full budget, all slow calls run to
        # completion). The fix must stay well below that -- bounded by
        # roughly timeout_seconds plus at most one extra in-flight probe
        # call's delay, not by the pattern count.
        old_unfixed_duration = NUM_PATTERNS * PROBE_DELAY
        assert wall_elapsed < old_unfixed_duration * 0.75, (
            f"probe loop took {wall_elapsed}s, which is not meaningfully "
            f"bounded below the old N*timeout_seconds duration of "
            f"{old_unfixed_duration}s -- the shared budget is not being "
            f"enforced"
        )
        assert wall_elapsed <= TIMEOUT_SECONDS + PROBE_DELAY + 1.5, (
            f"probe loop took {wall_elapsed}s, expected roughly bounded by "
            f"timeout_seconds={TIMEOUT_SECONDS}s"
        )

        # The main search's candidate is still returned -- the shared-budget
        # fix must not affect the main search's own results.
        assert len(candidates) == 1
        assert candidates[0] == tmp_path / "a.py"

        # The shared budget must have stopped the loop before every pattern
        # was probed -- not all NUM_PATTERNS calls should have completed.
        probe_calls_made = call_count["n"] - 1
        assert probe_calls_made < NUM_PATTERNS, (
            f"expected the shared budget to cut the probe loop short, but "
            f"all {probe_calls_made} patterns were probed"
        )


# ---------------------------------------------------------------------------
# R2 (post-merge review finding): a zero-match-pattern probe timeout is
# advisory-only and must never discard candidates the main search already
# found successfully within budget.
# ---------------------------------------------------------------------------


class TestZeroMatchProbeTimeoutPreservesMainSearch:
    def test_probe_timeout_preserves_main_search_candidates(
        self, search_engine, tmp_path
    ):
        """When the main search succeeds with real candidates but the
        zero-match-pattern probe subsequently times out, the function must
        return the main search's results normally (not an empty/timeout
        result), and must record a warning about the probe timeout."""
        (tmp_path / "a.py").write_text("password = 1\n")
        fake_match = _make_regex_match("a.py")
        main_result = _make_search_result([fake_match])

        async def _search_side_effect(*args: Any, **kwargs: Any) -> Any:
            # The main content search passes max_results=100_000; the
            # zero-match probe passes max_results=1. Use that to
            # distinguish which call this is.
            if kwargs.get("max_results") == 100_000:
                return main_result
            raise TimeoutError("zero-match-pattern probe timed out")

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(side_effect=_search_side_effect)

            candidates = search_engine._run_phase1_driver(
                tmp_path,
                "password",
                "content",
                ["**/*.py"],
                [],
                timeout_seconds=5,
            )

        # R2: the main search's real candidate must be preserved, not
        # discarded because the advisory probe timed out.
        assert len(candidates) == 1
        assert candidates[0] == tmp_path / "a.py"

        # A warning about the probe timeout must be recorded via the
        # established _last_phase1_warnings mechanism.
        warning_types = {w["type"] for w in search_engine._last_phase1_warnings}
        assert "zero_match_probe_timeout" in warning_types

    def test_probe_timeout_does_not_raise_phase1_timeout_error(
        self, search_engine, tmp_path
    ):
        """R2: run() must complete normally (no timeout/partial flags) when
        only the advisory probe times out, since the main search succeeded
        within budget."""
        (tmp_path / "a.py").write_text("password = 1\n")
        fake_match = _make_regex_match("a.py")
        main_result = _make_search_result([fake_match])

        async def _search_side_effect(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("max_results") == 100_000:
                return main_result
            raise TimeoutError("zero-match-pattern probe timed out")

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(side_effect=_search_side_effect)

            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex="password",
                evaluator_code=_DEFAULT_EVALUATOR,
                search_target="content",
                include_patterns=["**/*.py"],
                timeout_seconds=5,
            )

        assert "timeout" not in result
        assert "partial" not in result
        # Phase 1's main search found the file; use files_total (not
        # matches) since the rust-backend AST evaluator's own function
        # signature requirements are unrelated to this Phase-1 probe-
        # timeout fix and out of scope for this assertion.
        assert result["files_total"] == 1
