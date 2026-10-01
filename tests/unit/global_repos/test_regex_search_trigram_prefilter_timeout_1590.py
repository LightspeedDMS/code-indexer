"""Bug #1590: RegexSearchService.search()'s trigram pre-filter has no
timeout, letting Phase 1 of xray_search/xray_explore hang indefinitely.

This is the CORE reproduction for the bug: a REAL repository, a REAL
ripgrep binary, and a REAL, on-disk trigram index built via
TrigramIndexManager -- the ONLY thing simulated is a slow/blocking
TrigramIndexManager.query() call (a real method monkeypatched to sleep
past the deadline, exactly as the issue's own reproduction guidance
suggests), proving:

(a) pre-fix, the whole search() call ignores timeout_seconds entirely for
    the trigram pre-filter phase and takes the FULL block duration
    (structural gap: _prefilter_candidate_files is offloaded via
    anyio.to_thread.run_sync with no timeout at all around that hop);
(b) post-fix, search() returns within timeout_seconds + a small tolerance
    and raises TimeoutError -- the SAME sentinel the pre-existing ripgrep
    subprocess timeout already raises, so XRaySearchEngine's existing
    Phase-1-timeout handling (search_engine.py's `except TimeoutError`)
    picks it up unchanged.

No mocking of ripgrep, the trigram index build, or the trigram query
LOGIC -- only the specific TrigramIndexManager.query bound method is
wrapped with an artificial sleep to deterministically trigger the hang
this issue describes, per the task's explicit "real repo, real trigram
index, controllable slow point" reproduction requirement.
"""

from __future__ import annotations

import shutil
import time

import pytest

from code_indexer.global_repos.regex_search import (
    _MIN_PARSE_TIMEOUT_SECONDS,
    RegexSearchService,
)
from code_indexer.global_repos.trigram_index_manager import TrigramIndexManager

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None, reason="ripgrep required for regex search"
)

# Round 5 findings 1/2: shared wall-clock slack allowed on top of a test's
# TIMEOUT_SECONDS to absorb scheduling jitter (thread startup, GC pauses)
# without weakening the "was this actually bounded" assertion.
_TIMEOUT_TOLERANCE_SECONDS = 2.5


@pytest.fixture(autouse=True)
def _no_lazy_build(monkeypatch):
    # The index is built explicitly by these tests; disable the background
    # lazy rebuild so it cannot race the explicit build.
    monkeypatch.setenv("CIDX_TRIGRAM_LAZY_BUILD", "0")


def _build_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "auth.java").write_text("public class LSAuthenticator {}\n")
    (repo / "other.java").write_text("public class Widget {}\n")
    return repo


def _build_index(repo):
    mgr = TrigramIndexManager(repo / ".code-indexer" / "trigram_index")
    mgr.build(repo)
    return mgr


class TestTrigramPrefilterHangIsBounded:
    async def test_search_bounded_when_trigram_query_blocks(
        self, tmp_path, monkeypatch
    ):
        """Core reproduction: a real repo + real trigram index + real
        ripgrep, with a real sqlite3.connect() call inside
        TrigramIndexManager.query() monkeypatched to sleep past
        timeout_seconds -- the same reproduction technique already proven
        directly against TrigramIndexManager in
        test_trigram_index_manager_timeout_1590.py, exercised here through
        the full RegexSearchService.search() call path. search() must
        return within timeout_seconds + tolerance and raise TimeoutError --
        NOT silently take the full BLOCK_SECONDS and complete normally,
        which is what happens on the pre-fix code (proving the structural
        gap the issue describes: the prefilter hop has zero timeout of its
        own).

        BLOCK_SECONDS and TIMEOUT_SECONDS are local to this test: the
        artificial connect() delay (BLOCK_SECONDS) and the search's
        requested deadline (TIMEOUT_SECONDS) used both to drive the
        monkeypatch and to compute the wall-clock assertions below.
        """
        repo = _build_repo(tmp_path)
        _build_index(repo)

        import code_indexer.global_repos.trigram_index_manager as tim_mod

        real_connect = tim_mod.sqlite3.connect
        BLOCK_SECONDS = 3.0
        TIMEOUT_SECONDS = 1

        def _slow_connect(*args, **kwargs):
            time.sleep(BLOCK_SECONDS)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(tim_mod.sqlite3, "connect", _slow_connect)

        svc = RegexSearchService(repo)

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await svc.search(
                "LSAuthenticator", max_results=1000, timeout_seconds=TIMEOUT_SECONDS
            )
        elapsed = time.monotonic() - start

        assert elapsed < BLOCK_SECONDS, (
            f"search() took {elapsed:.2f}s -- expected to be bounded well "
            f"under the {BLOCK_SECONDS}s blocking trigram query, proving "
            f"the timeout genuinely fired mid-prefilter rather than "
            f"waiting for the slow call to finish"
        )
        assert elapsed <= TIMEOUT_SECONDS + 1.5, (
            f"search() took {elapsed:.2f}s, expected roughly bounded by "
            f"timeout_seconds={TIMEOUT_SECONDS}s"
        )

    async def test_search_bounded_when_trigram_exists_blocks(
        self, tmp_path, monkeypatch
    ):
        """Same reproduction, but the slow point is the sqlite3.connect()
        call inside TrigramIndexManager.exists() instead of .query() -- the
        OTHER call site the issue names as unbounded."""
        repo = _build_repo(tmp_path)
        _build_index(repo)

        import code_indexer.global_repos.trigram_index_manager as tim_mod

        real_connect = tim_mod.sqlite3.connect
        BLOCK_SECONDS = 3.0
        TIMEOUT_SECONDS = 1

        def _slow_connect(*args, **kwargs):
            time.sleep(BLOCK_SECONDS)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(tim_mod.sqlite3, "connect", _slow_connect)

        svc = RegexSearchService(repo)

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await svc.search(
                "LSAuthenticator", max_results=1000, timeout_seconds=TIMEOUT_SECONDS
            )
        elapsed = time.monotonic() - start

        assert elapsed < BLOCK_SECONDS
        assert elapsed <= TIMEOUT_SECONDS + 1.5, (
            f"search() took {elapsed:.2f}s, expected roughly bounded by "
            f"timeout_seconds={TIMEOUT_SECONDS}s"
        )

    async def test_shared_budget_across_exists_and_query_calls(
        self, tmp_path, monkeypatch
    ):
        """Code-review finding F1: _prefilter_candidate_files must carry ONE
        shared budget across its TWO internal calls (index.exists() then
        index.query()), not hand each one an independent fresh copy of
        timeout_seconds.

        Reproduction (mirrors the reviewer's own live reproduction
        exactly): EVERY sqlite3.connect() invocation -- both inside
        exists() and inside query() -- blocks for CONNECT_BLOCK_SECONDS
        (1.8s), against a TOTAL_TIMEOUT_SECONDS (2.0s) total budget.
        exists() alone comfortably completes within a full 2.0s budget
        (1.8s < 2.0s) -- consuming 1.8s of it. Pre-fix, query() then still
        receives its OWN FRESH 2.0s budget (the same static
        timeout_seconds value, not reduced by the 1.8s exists() already
        spent), so its 1.8s block also completes "successfully": total
        elapsed ~3.6s (1.8x the requested 2.0s deadline) with NO
        exception at all -- exactly the bug. Post-fix, query() receives
        only the STARVED remainder (~0.2s) of the SAME shared deadline
        exists() already ate into, so its watchdog fires well before the
        1.8s block completes, raising TimeoutError with total elapsed
        close to the original 2.0s budget.
        """
        repo = _build_repo(tmp_path)
        _build_index(repo)

        import code_indexer.global_repos.trigram_index_manager as tim_mod

        real_connect = tim_mod.sqlite3.connect
        CONNECT_BLOCK_SECONDS = 1.8
        TOTAL_TIMEOUT_SECONDS = 2.0
        call_count = {"n": 0}

        def _connect_side_effect(*args, **kwargs):
            call_count["n"] += 1
            time.sleep(CONNECT_BLOCK_SECONDS)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(tim_mod.sqlite3, "connect", _connect_side_effect)

        svc = RegexSearchService(repo)

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await svc.search(
                "LSAuthenticator",
                max_results=1000,
                timeout_seconds=TOTAL_TIMEOUT_SECONDS,
            )
        elapsed = time.monotonic() - start

        assert elapsed <= TOTAL_TIMEOUT_SECONDS + 1.5, (
            f"search() took {elapsed:.2f}s -- expected roughly bounded by "
            f"the shared TOTAL_TIMEOUT_SECONDS={TOTAL_TIMEOUT_SECONDS}s "
            f"budget across BOTH internal trigram calls, not "
            f"~2x{CONNECT_BLOCK_SECONDS}s from each getting its own "
            f"independent full copy of the budget"
        )
        assert call_count["n"] >= 2, (
            "test setup: expected both exists() and query() to reach sqlite3.connect()"
        )

    async def test_search_without_timeout_seconds_still_completes(self, tmp_path):
        """Regression: omitting timeout_seconds preserves prior behavior
        (no artificial bound applied when the caller specifies none)."""
        repo = _build_repo(tmp_path)
        _build_index(repo)
        svc = RegexSearchService(repo)

        result = await svc.search("LSAuthenticator", max_results=1000)
        assert {m.file_path for m in result.matches} == {"auth.java"}

    async def test_search_with_generous_timeout_returns_real_matches(self, tmp_path):
        """A generous timeout on a healthy trigram index must not
        spuriously fire or otherwise change search results."""
        repo = _build_repo(tmp_path)
        _build_index(repo)
        svc = RegexSearchService(repo)

        result = await svc.search(
            "LSAuthenticator", max_results=1000, timeout_seconds=60
        )
        assert {m.file_path for m in result.matches} == {"auth.java"}


def _build_many_candidates_repo(tmp_path, count):
    """Repo with ``count`` files that all trigram-match "TargetPatternCase"."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(count):
        (repo / f"file_{i}.java").write_text(
            f"public class TargetPatternCase{i} {{}}\n"
        )
    return repo


class TestPrefilterCandidateResolveLoopIsBounded:
    """Bug #1590 review round 3 finding B1: the loop in
    ``_prefilter_candidate_files`` that resolves each trigram-index
    candidate to an absolute path has NO deadline check of its own -- it
    can iterate up to ``_MAX_PREFILTER_CANDIDATES`` (8000) times unbounded,
    contradicting the "combined Phase 1 call cannot run past
    timeout_seconds end-to-end" claim. Reproduced by monkeypatching
    ``pathlib.Path.resolve`` (the real per-candidate call) to sleep.
    """

    CANDIDATE_COUNT = 100
    RESOLVE_BLOCK_SECONDS = 0.05
    TIMEOUT_SECONDS = 1

    async def test_search_bounded_when_resolving_candidate_paths_blocks(
        self, tmp_path, monkeypatch
    ):
        repo = _build_many_candidates_repo(tmp_path, self.CANDIDATE_COUNT)
        _build_index(repo)

        import code_indexer.global_repos.regex_search as rs_mod

        real_resolve = rs_mod.Path.resolve

        def _slow_resolve(self_path, *args, **kwargs):
            time.sleep(self.RESOLVE_BLOCK_SECONDS)
            return real_resolve(self_path, *args, **kwargs)

        monkeypatch.setattr(rs_mod.Path, "resolve", _slow_resolve)

        svc = RegexSearchService(repo)
        unbounded_worst_case = self.CANDIDATE_COUNT * self.RESOLVE_BLOCK_SECONDS

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await svc.search(
                "TargetPatternCase",
                max_results=1000,
                timeout_seconds=self.TIMEOUT_SECONDS,
            )
        elapsed = time.monotonic() - start

        assert elapsed < unbounded_worst_case, (
            f"search() took {elapsed:.2f}s -- expected bounded well under "
            f"the {unbounded_worst_case:.2f}s unbounded worst case, proving "
            f"the resolve loop's deadline check genuinely fired"
        )
        assert elapsed <= self.TIMEOUT_SECONDS + 2.5, (
            f"search() took {elapsed:.2f}s, expected roughly bounded by "
            f"timeout_seconds={self.TIMEOUT_SECONDS}s"
        )


class TestPrefilterSingleWedgedResolveIsBounded:
    """Bug #1590 review round 4 finding R1: B1's fix (see the class above)
    only checks the shared deadline BETWEEN iterations of the
    ``Path.resolve()`` loop in ``_prefilter_candidate_files`` -- a SINGLE
    wedged resolve call (e.g. the `hard` NFSv3 golden-repo mount that can
    block in uninterruptible kernel retry and never return -- this
    project's own documented failure mode) is still completely unbounded,
    because the deadline check only runs at the TOP of each loop
    iteration, not around the blocking call itself.

    A SINGLE-candidate repo makes this deterministic: the wedged resolve
    is unavoidably both the first AND the last iteration, so there is no
    "next iteration" left for the existing inter-iteration deadline check
    to ever fire on. Live investigation (before implementing the fix)
    showed this is worse than a bounded overrun: the loop finishes,
    returns the candidate anyway, ripgrep then runs (its own timeout
    already exhausted, so it gets only the ``_MIN_RIPGREP_TIMEOUT_SECONDS``
    floor) and finds the match, and ``_to_repo_relative`` resolves the SAME
    path a second time while parsing ripgrep's JSON output -- so the
    unpatched code doesn't just overrun, it completes ``search()``
    SUCCESSFULLY with no ``TimeoutError`` at all, after roughly ``2 *
    WEDGE_BLOCK_SECONDS``.
    """

    WEDGE_BLOCK_SECONDS = 3.0
    TIMEOUT_SECONDS = 1

    async def test_search_bounded_when_single_candidate_resolve_wedges(
        self, tmp_path, monkeypatch
    ):
        repo = _build_many_candidates_repo(tmp_path, 1)
        _build_index(repo)

        # Construct the service BEFORE monkeypatching Path.resolve so the
        # constructor's own `Path(repo_path).resolve()` call is unaffected.
        svc = RegexSearchService(repo)

        import code_indexer.global_repos.regex_search as rs_mod

        real_resolve = rs_mod.Path.resolve
        # Identify the wedged call by WHICH path it resolves, not by call
        # order -- the trigram index's own exists()/query() also call
        # Path.resolve() (on the index db file, already watchdog-protected
        # by _run_with_thread_watchdog) before the prefilter loop ever
        # runs, so counting calls would wedge the wrong one. Targeting the
        # exact candidate file path pins the wedge to
        # ``(self.repo_path / rel).resolve()`` inside the loop under test.
        wedge_target = repo / "file_0.java"

        def _wedge_one_candidate(self_path, *args, **kwargs):
            if self_path == wedge_target:
                time.sleep(self.WEDGE_BLOCK_SECONDS)
            return real_resolve(self_path, *args, **kwargs)

        monkeypatch.setattr(rs_mod.Path, "resolve", _wedge_one_candidate)

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await svc.search(
                "TargetPatternCase",
                max_results=1000,
                timeout_seconds=self.TIMEOUT_SECONDS,
            )
        elapsed = time.monotonic() - start

        assert elapsed < self.WEDGE_BLOCK_SECONDS, (
            f"search() took {elapsed:.2f}s -- expected bounded well under "
            f"the single {self.WEDGE_BLOCK_SECONDS:.2f}s wedged resolve "
            f"call (let alone the ~2x that call previously took to even "
            f"complete, without raising at all), proving the resolve "
            f"phase is now watchdog-protected against ONE stuck syscall "
            f"with no later iteration required to notice it"
        )
        assert elapsed <= self.TIMEOUT_SECONDS + 2.5, (
            f"search() took {elapsed:.2f}s, expected roughly bounded by "
            f"timeout_seconds={self.TIMEOUT_SECONDS}s"
        )


class TestSearchPathExistsCheckIsBounded:
    """Bug #1590 review round 5 finding 1: the ``search_path.exists()``
    check near the top of ``search()`` carried no timeout of its own. The
    round-4 comment there justified this by claiming ``deadline`` only
    becomes available "further down, inside the ripgrep-engine branch" --
    review round 5 proved that justification FALSE: ``deadline`` depends
    only on ``start_monotonic`` (computed two lines above the check) and
    ``timeout_seconds`` (already a parameter), neither of which has
    anything to do with which engine gets dispatched.

    This matters because ``regex_search`` is dispatched without an outer
    ``asyncio.wait_for`` (see ``_ASYNC_DISPATCH_TIMEOUT_EXEMPT_TOOLS`` in
    ``server/mcp/protocol.py``) -- ``search()``'s own internal budget is
    the ONLY bound. A wedged ``os.stat()`` here (e.g. against the `hard`
    NFSv3 golden-repo mount) would hold one of xray's 4 global
    concurrency slots forever -- the exact failure mode Bug #1590 exists
    to eliminate.

    Reproduced the same way the resolve-wedge tests above do: monkeypatch
    the real ``Path.exists`` to sleep only when called on the exact
    ``search_path`` under test.
    """

    WEDGE_BLOCK_SECONDS = 3.0
    TIMEOUT_SECONDS = 1

    async def test_search_bounded_when_search_path_exists_wedges(
        self, tmp_path, monkeypatch
    ):
        repo = _build_repo(tmp_path)

        # Construct BEFORE monkeypatching so the constructor's own
        # Path(repo_path).resolve() call is unaffected.
        svc = RegexSearchService(repo)

        import code_indexer.global_repos.regex_search as rs_mod

        real_exists = rs_mod.Path.exists
        wedge_target = repo

        def _wedge_exists(self_path, *args, **kwargs):
            if self_path == wedge_target:
                time.sleep(self.WEDGE_BLOCK_SECONDS)
            return real_exists(self_path, *args, **kwargs)

        monkeypatch.setattr(rs_mod.Path, "exists", _wedge_exists)

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await svc.search(
                "LSAuthenticator",
                max_results=1000,
                timeout_seconds=self.TIMEOUT_SECONDS,
            )
        elapsed = time.monotonic() - start

        assert elapsed < self.WEDGE_BLOCK_SECONDS, (
            f"search() took {elapsed:.2f}s -- expected bounded well under "
            f"the {self.WEDGE_BLOCK_SECONDS:.2f}s wedged "
            f"search_path.exists() call, proving the existence check is "
            f"now watchdog-protected against a stuck stat() rather than "
            f"waiting for it to finish"
        )
        assert elapsed <= self.TIMEOUT_SECONDS + _TIMEOUT_TOLERANCE_SECONDS, (
            f"search() took {elapsed:.2f}s, expected roughly bounded by "
            f"timeout_seconds={self.TIMEOUT_SECONDS}s"
        )

    async def test_search_without_timeout_seconds_exists_check_unaffected(
        self, tmp_path
    ):
        """Regression: omitting timeout_seconds must not apply any bound
        to the existence check -- unchanged prior behavior for callers
        with no deadline of their own."""
        repo = _build_repo(tmp_path)
        svc = RegexSearchService(repo)

        result = await svc.search("LSAuthenticator", max_results=1000)
        assert {m.file_path for m in result.matches} == {"auth.java"}


class TestParseResolvePhaseIsBounded:
    """Bug #1590 review round 5 finding 2: a second, previously
    undisclosed unprotected blocking call inside the very phase the
    round-4 R2 comment claimed was fully covered.

    ``_to_repo_relative`` (called once PER MATCH from
    ``_read_and_parse_ripgrep``, itself offloaded via
    ``anyio.to_thread.run_sync`` with NO timeout of its own) calls
    ``candidate.resolve()`` on the path ripgrep reported for that match --
    the IDENTICAL ``Path.resolve()``-on-NFS class of gap round 4's R1 fix
    addressed for the trigram pre-filter's resolve loop, but round 4 never
    touched this function.

    Reproduced by wedging the real ``Path.resolve()`` on the SECOND call
    for the matched file's path: the first call is the (now
    watchdog-protected, per R1) trigram pre-filter resolve, which must
    stay fast so the wedge lands specifically on the second, unprotected
    call inside ``_to_repo_relative`` during JSON parsing -- proving a
    single wedged resolve there absorbs the FULL wedge duration past an
    already-exhausted budget and, pre-fix, search() completes
    SUCCESSFULLY with no ``TimeoutError`` at all rather than bounding by
    ``timeout_seconds``.
    """

    WEDGE_BLOCK_SECONDS = 3.0
    TIMEOUT_SECONDS = 1

    async def test_search_bounded_when_parse_phase_resolve_wedges(
        self, tmp_path, monkeypatch
    ):
        repo = _build_repo(tmp_path)
        _build_index(repo)

        # Construct BEFORE monkeypatching so the constructor's own
        # Path(repo_path).resolve() call is unaffected.
        svc = RegexSearchService(repo)

        import code_indexer.global_repos.regex_search as rs_mod

        real_resolve = rs_mod.Path.resolve
        wedge_target = repo / "auth.java"
        call_count = {"n": 0}

        def _wedge_second_resolve(self_path, *args, **kwargs):
            if self_path == wedge_target:
                call_count["n"] += 1
                if call_count["n"] >= 2:
                    time.sleep(self.WEDGE_BLOCK_SECONDS)
            return real_resolve(self_path, *args, **kwargs)

        monkeypatch.setattr(rs_mod.Path, "resolve", _wedge_second_resolve)

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await svc.search(
                "LSAuthenticator",
                max_results=1000,
                timeout_seconds=self.TIMEOUT_SECONDS,
            )
        elapsed = time.monotonic() - start

        assert elapsed < self.WEDGE_BLOCK_SECONDS, (
            f"search() took {elapsed:.2f}s -- expected bounded well under "
            f"the single {self.WEDGE_BLOCK_SECONDS:.2f}s wedged resolve "
            f"call inside the parse phase's _to_repo_relative, proving "
            f"that call is now watchdog-protected against ONE stuck "
            f"syscall rather than silently absorbing it and completing "
            f"successfully with no TimeoutError at all"
        )
        assert elapsed <= self.TIMEOUT_SECONDS + _TIMEOUT_TOLERANCE_SECONDS, (
            f"search() took {elapsed:.2f}s, expected roughly bounded by "
            f"timeout_seconds={self.TIMEOUT_SECONDS}s"
        )
        assert call_count["n"] >= 2, (
            "test setup: expected the target path to be resolved at least "
            "twice (once in the prefilter resolve loop, once in "
            "_to_repo_relative during parsing)"
        )

    async def test_search_without_timeout_seconds_parse_phase_unaffected(
        self, tmp_path
    ):
        """Regression: omitting timeout_seconds must not apply any bound
        to the parse-phase resolve -- unchanged prior behavior for callers
        with no deadline of their own."""
        repo = _build_repo(tmp_path)
        _build_index(repo)
        svc = RegexSearchService(repo)

        result = await svc.search("LSAuthenticator", max_results=1000)
        assert {m.file_path for m in result.matches} == {"auth.java"}


class TestParseResolvePhaseFloorNotZero:
    """Bug #1590 review round 6: a zero-budget floor bug in the parse
    phase's watchdog budget causes a fully successful, already-completed
    ripgrep run to be spuriously discarded as a ``TimeoutError``.

    Mechanism: the ripgrep-subprocess-phase timeout is computed as
    ``max(_MIN_RIPGREP_TIMEOUT_SECONDS, math.ceil(remaining))`` -- rounding
    UP, so ripgrep can legitimately run up to ~1 extra second past the true
    shared ``deadline``. When ripgrep finishes inside that generously
    rounded-up allowance but AFTER the true deadline has technically
    elapsed, the parse phase computes its own budget as ``deadline -
    time.monotonic()``, which is already <= 0. Pre-fix, that value is
    handed to ``_run_with_thread_watchdog`` verbatim (floored only at
    0.0), so ``Thread.join(timeout=0)`` is called on a worker thread that
    has barely been scheduled -- it can never "win" even though the real
    parse work (a handful of matches, tiny JSON) completes in single-digit
    milliseconds.

    Reproduced with a REAL on-disk trigram index and REAL ripgrep, per the
    round-5 reviewer's own live numbers: a real
    ``TrigramIndexManager.query()`` call monkeypatched to sleep just UNDER
    the shared ``TIMEOUT_SECONDS`` budget (1.985s against 2.0s) so no
    earlier phase raises ITS OWN ``TimeoutError`` -- the query's own
    internal watchdog receives close to the full remaining budget and the
    sleep finishes comfortably inside it. What follows (the real ripgrep
    subprocess + the real JSON parse) is fast, unmonkeypatched code that
    should complete successfully; pre-fix, it does complete, but is
    discarded as a timeout anyway.
    """

    TIMEOUT_SECONDS = 2.0
    QUERY_SLEEP_SECONDS = 1.985

    async def test_search_succeeds_when_parse_phase_starts_after_deadline_elapsed(
        self, tmp_path, monkeypatch
    ):
        repo = _build_repo(tmp_path)
        _build_index(repo)

        import code_indexer.global_repos.trigram_index_manager as tim_mod

        real_query = tim_mod.TrigramIndexManager.query
        sleep_seconds = self.QUERY_SLEEP_SECONDS

        def _slow_query(self, *args, **kwargs):
            time.sleep(sleep_seconds)
            return real_query(self, *args, **kwargs)

        monkeypatch.setattr(tim_mod.TrigramIndexManager, "query", _slow_query)

        svc = RegexSearchService(repo)

        result = await svc.search(
            "LSAuthenticator",
            max_results=1000,
            timeout_seconds=self.TIMEOUT_SECONDS,
        )

        assert {m.file_path for m in result.matches} == {"auth.java"}, (
            "search() discarded a fully successful, already-completed "
            "result as a spurious TimeoutError -- the parse phase's "
            "post-deadline watchdog budget was zero/near-zero purely "
            "because an earlier phase's ceil()-rounded-up ripgrep "
            "allowance let real wall-clock time run past the shared "
            "deadline, even though the real parse work itself completed "
            "in milliseconds"
        )


class TestPrefilterResolvePhaseFloorNotZero:
    """Bug #1590 review round 6: the SAME zero-budget floor artifact,
    pre-existing since round 4, at the trigram pre-filter's
    candidate-path resolve phase (``_prefilter_candidate_files``'s
    ``_resolve_candidates`` watchdog call). Reproduced the same way, tuned
    (per the round-5 reviewer's own live numbers) so the deadline has
    already technically elapsed by the time THIS phase -- not the later
    ripgrep/parse phases -- computes its watchdog budget.

    Review round 7 finding 2: the ORIGINAL version of this test (imports
    ``_MIN_PARSE_TIMEOUT_SECONDS`` from
    ``code_indexer.global_repos.regex_search`` at the top of this file)
    asserted on the final search() outcome (matches returned vs.
    TimeoutError raised), which depends on a genuine scheduling RACE --
    ``Thread.join(timeout=X)`` where X is an unfloored, near-zero
    ``remaining`` value can "win" or "lose" against a resolve loop that
    completes in low-single-digit milliseconds purely based on OS
    scheduling jitter. The round-6/7 reviewer measured this at only 7/12
    (58%) discriminating with the fix genuinely reverted, and found it
    ALSO goes red 12/12 when the unrelated parse-phase floor (the sibling
    ``TestParseResolvePhaseFloorNotZero``'s own site) is reverted instead
    -- i.e. it neither reliably caught its own bug nor localized to its
    own site.

    Fixed (option A from the round-7 reviewer) by asserting directly and
    deterministically on the budget value itself: the shared
    ``_run_with_thread_watchdog`` is wrapped by a PASS-THROUGH SPY that
    captures the ``timeout_seconds`` argument passed specifically for the
    ``"trigram_prefilter_resolve"`` label, then unconditionally delegates
    to the real implementation with the same arguments -- no behavior is
    faked or skipped, every phase still executes for real. This removes
    the race entirely -- ``time.sleep(1.995)`` against a 2.0s deadline
    mathematically guarantees the raw ``remaining`` computed immediately
    afterward is in ``[0.0, ~0.005]`` (sleep is a floor, not an exact
    duration, so real elapsed time can only be >= 1.995s), which is
    always < 1.0 on a genuinely-reverted floor and always >= 1.0
    (``_MIN_PARSE_TIMEOUT_SECONDS``) on the fix -- a fixed 0.995s+ margin,
    not a coin flip. It also inherently localizes: the capture only fires
    for the ``"trigram_prefilter_resolve"`` label, and the downstream
    ``svc.search()`` call is allowed to raise ``TimeoutError`` (caught
    and ignored) without invalidating the assertion, so reverting the
    UNRELATED parse-phase floor -- a different label, a different call
    site, possibly raising its own downstream TimeoutError -- cannot
    change what was already captured here.
    """

    TIMEOUT_SECONDS = 2.0
    QUERY_SLEEP_SECONDS = 1.995

    async def test_resolve_phase_watchdog_budget_is_floored_not_near_zero(
        self, tmp_path, monkeypatch
    ):
        repo = _build_repo(tmp_path)
        _build_index(repo)

        import code_indexer.global_repos.trigram_index_manager as tim_mod

        real_query = tim_mod.TrigramIndexManager.query
        sleep_seconds = self.QUERY_SLEEP_SECONDS

        def _slow_query(self, *args, **kwargs):
            time.sleep(sleep_seconds)
            return real_query(self, *args, **kwargs)

        monkeypatch.setattr(tim_mod.TrigramIndexManager, "query", _slow_query)

        real_watchdog = tim_mod._run_with_thread_watchdog
        captured_budgets: list = []

        def _capturing_watchdog(work, timeout_seconds, warning_label, holder=None):
            if warning_label == "trigram_prefilter_resolve":
                captured_budgets.append(timeout_seconds)
            return real_watchdog(work, timeout_seconds, warning_label, holder)

        monkeypatch.setattr(tim_mod, "_run_with_thread_watchdog", _capturing_watchdog)

        svc = RegexSearchService(repo)

        try:
            await svc.search(
                "LSAuthenticator",
                max_results=1000,
                timeout_seconds=self.TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # An UNRELATED downstream phase (ripgrep subprocess / parse)
            # may independently raise on this same shared deadline
            # depending on unrelated code paths -- irrelevant to what
            # this test asserts about the resolve phase's OWN watchdog
            # budget, already captured above before any such exception.
            pass

        assert captured_budgets, (
            "the trigram_prefilter_resolve phase's watchdog was never "
            "invoked -- test setup no longer reaches the phase under test"
        )
        assert captured_budgets[0] >= _MIN_PARSE_TIMEOUT_SECONDS, (
            f"trigram-prefilter-resolve phase watchdog budget was "
            f"{captured_budgets[0]:.6f}s, below the "
            f"_MIN_PARSE_TIMEOUT_SECONDS={_MIN_PARSE_TIMEOUT_SECONDS}s floor "
            f"-- query() consuming {sleep_seconds}s of the "
            f"{self.TIMEOUT_SECONDS}s shared deadline left this phase's raw "
            "remaining budget near-zero, and it was handed to "
            "Thread.join() nearly verbatim instead of being floored, which "
            "can spuriously discard an already-completed resolve as a "
            "TimeoutError"
        )
