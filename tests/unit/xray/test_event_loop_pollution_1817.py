"""Regression test for Bug #1817: asyncio event-loop-policy pollution.

Root cause: ``XRaySearchEngine._probe_zero_match_patterns_content`` drives an
async call through ``_run_async_in_sync``
(``src/code_indexer/xray/search_engine.py``). When no event loop is already
running on the calling thread -- the normal case for a synchronous pytest
test on the main thread -- ``_run_async_in_sync`` falls back to
``asyncio.run(coro)``.

CPython's ``asyncio.run()`` unconditionally clears the *calling thread's*
event loop in its ``finally`` block (``events.set_event_loop(None)``) once
the coroutine completes, regardless of what the loop looked like beforehand.
Without cleanup, any LATER code on the same thread that calls the deprecated
``asyncio.get_event_loop()`` API breaks: that API only auto-creates a fresh
loop the *very first* time it is ever invoked on a thread (an internal
``_set_called`` sentinel flips permanently True the first time any loop is
set, whether via ``asyncio.run()`` or otherwise). Once ``_set_called`` is
already True and the loop has been cleared to ``None``, every subsequent
``asyncio.get_event_loop()`` call raises:

    RuntimeError: There is no current event loop in thread 'MainThread'.

This is exactly what ``tests/unit/xray/test_zero_match_probe_read_capped_1601.py``
(alphabetically the last file collected under ``tests/unit/xray/``) leaves
behind for 16 tests across 6 files under ``tests/unit/server/mcp/`` that
drive ``handle_regex_search`` via ``asyncio.get_event_loop().run_until_complete(...)``,
when the combined selection ``pytest tests/unit/xray/ tests/unit/server/mcp/``
runs.

The two tests below reproduce that exact shape -- a leak in one test
surfacing in a LATER, separate test -- entirely in-process (no subprocess,
no second file). ``test_1_probe_call_drives_the_asyncio_run_fallback``
performs the same production call the real offending test makes;
``test_2_a_later_test_still_has_a_usable_event_loop`` is a wholly separate
test function that must run afterward (pytest executes tests in declaration
order in this repo -- ``pytest-randomly`` is not installed) and asserts the
loop was NOT left broken for it. Splitting the assertion into a second test
function is deliberate: the guard fixture's cleanup runs at TEARDOWN of the
first test (after its body returns), so only a second, later test can
observe whether that cleanup actually restored a usable loop. Both tests
skip together (module-level ``pytest.importorskip``) so ``test_2`` never
runs standalone -- and thus never trivially "passes" -- when ``test_1`` (the
actual leak trigger) was skipped. This proves the
``tests/unit/xray/conftest.py`` autouse guard fixture restores a usable
event loop after every xray test (mirrors the project's existing precedent
for this exact class of bug: ``tests/unit/remote/conftest.py``'s
``cleanup_event_loop`` fixture).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("tree_sitter_languages")


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
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


class TestEventLoopNotLeakedAcrossTests:
    """Bug #1817: a leak in one test must not survive into a later test."""

    def test_1_probe_call_drives_the_asyncio_run_fallback(
        self, search_engine, tmp_path
    ) -> None:
        """Reproduces the exact production call the real offending test
        (test_zero_match_probe_read_capped_1601.py) makes: a synchronous
        call into XRaySearchEngine that internally falls back to
        asyncio.run() because no loop is running on the main thread."""
        from code_indexer.global_repos.regex_search import RegexMatch

        fake_match = RegexMatch(
            file_path="a.py", line_number=1, column=1, line_content="x"
        )
        fake_result = _make_search_result([fake_match], read_capped=True)

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(return_value=fake_result)

            search_engine._probe_zero_match_patterns_content(tmp_path, ["**/*.py"])

    def test_2_a_later_test_still_has_a_usable_event_loop(self) -> None:
        """This is the exact call pattern the 16 Bug #1817 victims rely on
        (asyncio.get_event_loop().run_until_complete(...)). It must not
        raise "There is no current event loop in thread 'MainThread'" as a
        result of the PRECEDING test's asyncio.run() fallback."""
        loop = asyncio.get_event_loop()
        assert loop is not None
        assert not loop.is_closed()
