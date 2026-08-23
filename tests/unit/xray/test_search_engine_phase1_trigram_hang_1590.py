"""Bug #1590 AC4: a job stuck in Phase 1 (candidate-file selection) must
not hold its xray cell-limiter slot forever.

server/mcp/handlers/xray.py's job bodies acquire one of only 4 global xray
concurrency slots (a ResizableLimiter) BEFORE calling
XRaySearchEngine.run(), and release it in a `finally` block once run()
returns. This test reproduces that exact acquire -> run() -> release
pattern with a REAL ResizableLimiter and a REAL, on-disk trigram index
whose sqlite3.connect() is monkeypatched to block (simulating an
unresponsive mount / contended I/O -- the mechanism Bug #1590 describes),
proving that Bug #1590's AC1 fix (bounding Phase 1's trigram pre-filter by
timeout_seconds) is what makes the slot release effectively prompt: no
separate cancel_job -> interrupt-registration mechanism is needed, because
the job itself can no longer hang past timeout_seconds in the first place.
This is the pragmatic tradeoff documented in
TrigramIndexManager._run_with_thread_watchdog's docstring.
"""

from __future__ import annotations

import time

import pytest

_DEFAULT_EVALUATOR = (
    'matches = [{"line_number": mp["line_number"]} for mp in match_positions]\n'
    'return {"matches": matches, "value": None}'
)

BLOCK_SECONDS = 3.0
TIMEOUT_SECONDS = 1


@pytest.fixture
def search_engine():
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


def _build_repo_with_index(tmp_path):
    from code_indexer.global_repos.trigram_index_manager import TrigramIndexManager

    (tmp_path / "auth.py").write_text("class LSAuthenticator: pass\n")
    mgr = TrigramIndexManager(tmp_path / ".code-indexer" / "trigram_index")
    mgr.build(tmp_path)


def _install_slow_sqlite_connect(monkeypatch):
    """Monkeypatch sqlite3.connect inside trigram_index_manager to block
    for BLOCK_SECONDS -- simulates an unresponsive mount / contended I/O."""
    import code_indexer.global_repos.trigram_index_manager as tim_mod

    real_connect = tim_mod.sqlite3.connect

    def _slow_connect(*args, **kwargs):
        time.sleep(BLOCK_SECONDS)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(tim_mod.sqlite3, "connect", _slow_connect)


class TestPhase1TrigramHangReleasesLimiterSlot:
    def test_stuck_trigram_prefilter_releases_limiter_slot_within_timeout(
        self, search_engine, tmp_path, monkeypatch
    ):
        from code_indexer.server.services.resizable_limiter import ResizableLimiter

        _build_repo_with_index(tmp_path)
        _install_slow_sqlite_connect(monkeypatch)

        # A single-slot limiter, mirroring xray.py's real _xray_cell_limiter
        # acquire-before-Phase1 / release-in-finally pattern.
        limiter = ResizableLimiter(initial=1, k_min=1, k_max=1)
        assert limiter.acquire(timeout=float(TIMEOUT_SECONDS)) is True, (
            "test setup: slot must be free initially"
        )

        start = time.monotonic()
        try:
            result = search_engine.run(
                repo_path=tmp_path,
                driver_regex="LSAuthenticator",
                evaluator_code=_DEFAULT_EVALUATOR,
                search_target="content",
                timeout_seconds=TIMEOUT_SECONDS,
            )
        finally:
            limiter.release()
        elapsed = time.monotonic() - start

        # AC2: the trigram-prefilter timeout must surface through the SAME
        # documented partial/timeout result shape Bug #1598 already
        # established for any other Phase 1 timeout -- never a raw
        # exception, never a different shape.
        assert result["partial"] is True
        assert result["timeout"] is True

        assert elapsed < BLOCK_SECONDS, (
            f"run() took {elapsed:.2f}s -- expected to be bounded well "
            f"under the {BLOCK_SECONDS}s blocking trigram connect(), "
            f"proving the whole job (and its held limiter slot) is "
            f"unblocked well before the underlying I/O would ever resolve"
        )
        assert elapsed <= TIMEOUT_SECONDS + 2.0, (
            f"run() took {elapsed:.2f}s, expected roughly bounded by "
            f"timeout_seconds={TIMEOUT_SECONDS}s"
        )

        # AC4: the slot must be immediately re-acquirable right after
        # run() returns -- proving it was genuinely released, not leaked
        # (a subsequent cancel_job call on this job_id would find the slot
        # already free rather than waiting for a server restart).
        assert limiter.acquire(timeout=0.1) is True, (
            "xray cell-limiter slot was not released promptly after the "
            "Phase-1-stuck job's run() call returned"
        )
