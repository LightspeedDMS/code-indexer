"""Bug #1611: RegexSearchService._search_python_multiline has NO timeout
enforcement at all.

The pure-Python ``re.DOTALL`` fallback used when ``multiline=True`` on the
grep engine (or when ripgrep is unavailable) performs its own synchronous
``os.walk`` + per-file ``open()``/``read()`` + ``compiled.finditer()`` loop
with no time budget whatsoever. A slow multiline regex (catastrophic
backtracking) or a large repo scanned via this fallback can run unbounded,
regardless of the caller's requested ``timeout_seconds``. This was
explicitly scoped OUT of bug #1598's Phase 1 timeout remediation.

This test proves the fix with REAL, un-mocked work: real files on disk and
a genuinely expensive (polynomial, not exponential -- deterministic and
bounded) regex scan. ``.*LITERAL`` against a large non-matching haystack is
a well-known O(n^2) case for Python's backtracking ``re`` engine (no
literal anchor to short-circuit the scan, and no memoization across start
positions) -- verified empirically: a single 40,000-character file with
this pattern takes ~0.4s of REAL CPU time to scan. No ``time.sleep`` or
mocked delay is used anywhere in this reproduction.
"""

from __future__ import annotations

import time

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService

# A deliberately expensive (but polynomial, hence bounded and deterministic)
# pattern: DOTALL ".*LITERAL" against a haystack that never contains LITERAL
# forces Python's backtracking `re` engine to rescan from every start
# position -- O(n^2) -- without any risk of exponential (unbounded) blowup.
_SLOW_PATTERN = r".*ZZZ_NOT_PRESENT_MARKER_ZZZ"

# Empirically measured (see module docstring): ~0.4s of real CPU time per
# file at this size. 20 such files therefore take ~8s to scan unbounded --
# comfortably longer than the timeout used below, proving a genuine
# mid-walk cutoff rather than a coincidental near-instant completion.
_SLOW_FILE_CONTENT = "A" * 40_000
_SLOW_FILE_COUNT = 20

# Real wall-clock budget handed to the fallback. Must be well under the
# ~8s unbounded full-scan duration above, and comfortably larger than one
# single file's ~0.4s scan cost (so the cutoff is proven to happen after
# some files were scanned, not merely as an instant no-op).
_TIMEOUT_SECONDS = 2

# Generous upper bound on how long the bounded call itself is allowed to
# run: timeout + at most one in-flight file's worst-case scan time + a
# comfortable scheduling-jitter margin.
_MAX_ALLOWED_ELAPSED_SECONDS = _TIMEOUT_SECONDS + 3.0

# Lower bound (as a fraction of the configured timeout budget) an
# implementation must actually take before raising TimeoutError. Catches
# an implementation that raises almost immediately while ignoring
# timeout_seconds entirely (e.g. a deadline computed without adding the
# budget) -- such a mutant returns in ~0.2ms and would otherwise pass
# every upper-bound-only assertion in this file.
_MIN_TIMEOUT_RATIO = 0.9

# Tiny fixture content used ONLY to prove the "no timeout_seconds means
# unbounded behavior is preserved" backward-compatibility contract. That
# assertion (return shape / total==0 on a non-matching pattern) needs no
# expensive corpus at all -- it is exercised identically, in ~1ms, on 2
# small files instead of running the real ~8-10s unbounded scan over
# _SLOW_FILE_COUNT x _SLOW_FILE_CONTENT. Using the slow corpus here was the
# sole reason this file exceeded fast-automation.sh's 15s per-test timeout
# (measured 8.9-10.2s for this one test alone).
_FAST_FILE_CONTENT = "A" * 1_000
_FAST_FILE_COUNT = 2


@pytest.fixture
def slow_repo(tmp_path):
    """A repo of many files, each real and disk-backed, each expensive to
    scan with ``_SLOW_PATTERN`` under DOTALL -- see module docstring."""
    for i in range(_SLOW_FILE_COUNT):
        (tmp_path / f"slow_{i}.txt").write_text(_SLOW_FILE_CONTENT)
    return tmp_path


@pytest.fixture
def fast_repo(tmp_path):
    """A small, cheap-to-scan repo -- used only by the backward-compat test
    that asserts omitting timeout_seconds preserves unbounded behavior. No
    slow/expensive corpus is needed to prove that contract; see
    ``_FAST_FILE_CONTENT`` above."""
    for i in range(_FAST_FILE_COUNT):
        (tmp_path / f"fast_{i}.txt").write_text(_FAST_FILE_CONTENT)
    return tmp_path


@pytest.fixture
def service(slow_repo, monkeypatch):
    # NOTE: this patches shutil.which so RegexSearchService._detect_search_engine
    # reports "ripgrep" (service._search_engine == "ripgrep"), even though
    # every test in this file calls _search_python_multiline / _search_grep
    # DIRECTLY -- bypassing the engine dispatch in search() (the `if
    # self._search_engine == "ripgrep": ... else: self._search_grep(...)`
    # branch) entirely. That makes the reported engine irrelevant to what
    # these tests actually exercise (the grep/Python-multiline fallback
    # path); it is harmless, just not indicative of engine selection here.
    monkeypatch.setattr(
        "code_indexer.global_repos.regex_search.shutil.which",
        lambda cmd: "/usr/bin/rg" if cmd == "rg" else None,
    )
    return RegexSearchService(slow_repo)


@pytest.fixture
def fast_service(fast_repo, monkeypatch):
    monkeypatch.setattr(
        "code_indexer.global_repos.regex_search.shutil.which",
        lambda cmd: "/usr/bin/rg" if cmd == "rg" else None,
    )
    return RegexSearchService(fast_repo)


class TestPythonMultilineTimeoutEnforcement:
    """AC1/AC2 (#1611): _search_python_multiline must accept
    timeout_seconds and enforce it via a real elapsed-time check that
    fires before the whole (real, expensive) walk can complete."""

    def test_timeout_fires_before_full_unbounded_scan_completes(self, service):
        """Discriminating case: with NO timeout enforcement (pre-fix code,
        or a fix that merely accepts-but-ignores the parameter), this call
        runs to completion and takes the FULL ~8s unbounded scan duration
        with no exception. Post-fix, it must raise TimeoutError and return
        in well under that full duration -- proving the per-file elapsed
        check genuinely stopped the walk mid-scan rather than merely
        capping the eventual return value."""
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            service._search_python_multiline(
                pattern=_SLOW_PATTERN,
                search_path=service.repo_path,
                include_patterns=None,
                exclude_patterns=None,
                case_sensitive=True,
                max_results=100,
                timeout_seconds=_TIMEOUT_SECONDS,
            )
        elapsed = time.monotonic() - start

        assert elapsed <= _MAX_ALLOWED_ELAPSED_SECONDS, (
            f"_search_python_multiline took {elapsed:.2f}s -- expected to "
            f"be bounded well under the full unbounded-scan duration "
            f"(~{_SLOW_FILE_COUNT * 0.4:.1f}s), proving the timeout "
            f"genuinely fired mid-walk rather than letting the scan run "
            f"unbounded to completion"
        )
        # NOTE (code review finding): a naive `assert elapsed >= 0.3` with
        # a comment claiming it proved "at least one real file's worth of
        # work" happened before the raise previously lived here. That
        # specific assertion was tautological given this test's fixed 2s
        # deadline (`start + _TIMEOUT_SECONDS`): the per-file check only
        # raises once `time.monotonic() >= deadline`, so `elapsed` is
        # guaranteed to be >= 2s (hence also >= 0.3s) by construction
        # alone, regardless of whether the timeout budget was honored at
        # all -- so it was removed.
        #
        # A real lower bound tied to the actual budget still has
        # discriminating value, though: an implementation that raises
        # TimeoutError without honoring timeout_seconds at all (e.g. a
        # deadline computed without adding the budget, as in
        # `timeout_seconds=0`) returns in ~0.2ms and would otherwise pass
        # every upper-bound assertion in this test.
        assert elapsed >= _TIMEOUT_SECONDS * _MIN_TIMEOUT_RATIO, (
            f"_search_python_multiline took only {elapsed:.4f}s -- expected "
            f"at least ~{_TIMEOUT_SECONDS}s (the configured timeout budget), "
            f"proving the timeout_seconds value itself was honored rather "
            f"than an implementation that raises TimeoutError almost "
            f"immediately while ignoring the requested budget"
        )

    def test_no_timeout_means_unbounded_behavior_preserved(self, fast_service):
        """Backward compatibility: omitting timeout_seconds (default None)
        must preserve the exact prior behavior -- the scan runs to genuine
        completion with no TimeoutError. Uses the tiny ``fast_service``
        fixture rather than the shared slow corpus: this assertion only
        needs to observe the return shape / no-timeout behavior, which a
        2-file x 1KB fixture proves identically to the 20-file x 40KB one,
        in ~1ms instead of ~9-10s (the sole cause of this file exceeding
        fast-automation.sh's 15s per-test timeout)."""
        matches, total = fast_service._search_python_multiline(
            pattern=_SLOW_PATTERN,
            search_path=fast_service.repo_path,
            include_patterns=None,
            exclude_patterns=None,
            case_sensitive=True,
            max_results=100,
        )

        assert total == 0  # pattern never matches by construction
        assert matches == []


class TestSearchGrepForwardsTimeoutToPythonMultiline:
    """AC3 (#1611): _search_grep must forward timeout_seconds into
    _search_python_multiline when dispatching for multiline=True -- the
    fallback used when ripgrep is unavailable is otherwise permanently
    unbounded no matter what the caller (search()) requested.

    Genuine end-to-end reproduction through the REAL async dispatch path
    (``_search_grep`` -> ``anyio.to_thread.run_sync`` -> the real
    ``_search_python_multiline``, using the same real slow repo/pattern as
    above) -- nothing in ``RegexSearchService`` is mocked or stubbed.
    Forwarding is observed indirectly but unambiguously: if
    ``timeout_seconds`` were NOT reaching ``_search_python_multiline`` (the
    pre-fix behavior), these calls would run to full completion with no
    ``TimeoutError`` and take the full ~8s unbounded-scan duration; post-fix
    they must raise well within the small bound configured below.
    """

    @pytest.mark.asyncio
    async def test_explicit_timeout_seconds_is_forwarded(self, service):
        """An explicit timeout_seconds must reach and be enforced by the
        real _search_python_multiline fallback."""
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await service._search_grep(
                _SLOW_PATTERN,
                service.repo_path,
                None,
                None,
                True,
                0,
                100,
                _TIMEOUT_SECONDS,
                multiline=True,
            )
        elapsed = time.monotonic() - start

        assert elapsed <= _MAX_ALLOWED_ELAPSED_SECONDS

    @pytest.mark.asyncio
    async def test_none_timeout_seconds_defaults_to_default_search_timeout(
        self, service, monkeypatch
    ):
        """When the caller passes timeout_seconds=None, _search_grep must
        NOT forward a bare None (which would leave the fallback fully
        unbounded again) -- it must apply the same
        DEFAULT_SEARCH_TIMEOUT_SECONDS fallback the non-multiline grep
        path already applies (see the pre-existing
        ``timeout = timeout_seconds or DEFAULT_SEARCH_TIMEOUT_SECONDS``
        line), so this fallback is ALWAYS bounded even when the caller
        supplies no explicit deadline. DEFAULT_SEARCH_TIMEOUT_SECONDS
        (normally 300s) is patched down to the same small test bound so
        the real enforcement can be observed without a 300s-long test."""
        monkeypatch.setattr(
            "code_indexer.global_repos.regex_search.DEFAULT_SEARCH_TIMEOUT_SECONDS",
            _TIMEOUT_SECONDS,
        )

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await service._search_grep(
                _SLOW_PATTERN,
                service.repo_path,
                None,
                None,
                True,
                0,
                100,
                None,
                multiline=True,
            )
        elapsed = time.monotonic() - start

        assert elapsed <= _MAX_ALLOWED_ELAPSED_SECONDS
