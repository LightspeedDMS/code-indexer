"""Tests for Bug #982: _run_phase1_driver must reuse RegexSearchService.

Verifies that XRaySearchEngine._run_phase1_driver delegates content search
to RegexSearchService instead of the inline re.compile + rglob driver.

Search target "filename" retains the inline path-match because RegexSearchService
has no filename-search mode (content-only service).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _make_regex_match(file_path: str, line_number: int = 1, line_content: str = "x"):
    """Build a minimal RegexMatch-compatible object for use in tests."""
    from code_indexer.global_repos.regex_search import RegexMatch

    return RegexMatch(
        file_path=file_path,
        line_number=line_number,
        column=1,
        line_content=line_content,
    )


def _make_search_result(matches):
    """Build a minimal RegexSearchResult-compatible object."""
    from code_indexer.global_repos.regex_search import RegexSearchResult

    return RegexSearchResult(
        matches=matches,
        total_matches=len(matches),
        truncated=False,
        search_engine="ripgrep",
        search_time_ms=0.0,
    )


@pytest.fixture
def search_engine(tmp_path):
    """XRaySearchEngine instance; skip if tree-sitter extras not installed."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


class TestPhase1DriverUsesRegexSearchService:
    """_run_phase1_driver must call RegexSearchService.search for content target."""

    def test_phase1_driver_calls_regex_search_service_for_content(
        self, search_engine, tmp_path
    ):
        """Content-target Phase 1 must invoke RegexSearchService.search, not re.compile."""
        (tmp_path / "a.py").write_text("password = 1\n")

        fake_match = _make_regex_match(
            "a.py", line_number=1, line_content="password = 1"
        )
        fake_result = _make_search_result([fake_match])

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(return_value=fake_result)

            candidates = search_engine._run_phase1_driver(
                tmp_path, "password", "content", [], []
            )

        MockService.assert_called_once_with(tmp_path)
        instance.search.assert_called_once()
        call_kwargs = instance.search.call_args
        assert call_kwargs.kwargs.get("pattern") == "password" or (
            len(call_kwargs.args) > 0 and call_kwargs.args[0] == "password"
        )
        assert tmp_path in candidates or any(tmp_path / "a.py" == p for p in candidates)

    def test_phase1_driver_returns_list_of_paths(self, search_engine, tmp_path):
        """_run_phase1_driver must return a list of Path objects."""
        (tmp_path / "b.py").write_text("secret = 2\n")

        fake_match = _make_regex_match("b.py", line_number=1)
        fake_result = _make_search_result([fake_match])

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(return_value=fake_result)

            candidates = search_engine._run_phase1_driver(
                tmp_path, "secret", "content", [], []
            )

        assert isinstance(candidates, list)
        assert all(isinstance(p, Path) for p in candidates)


class TestPhase1DriverEmptyResults:
    """_run_phase1_driver returns empty list when RegexSearchService finds nothing."""

    def test_phase1_driver_handles_empty_match_list(self, search_engine, tmp_path):
        """When RegexSearchService returns no matches, candidate list is empty."""
        fake_result = _make_search_result([])

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(return_value=fake_result)

            candidates = search_engine._run_phase1_driver(
                tmp_path, "NOTHING_MATCHES", "content", [], []
            )

        assert candidates == []


class TestPhase1DriverDeduplication:
    """_run_phase1_driver returns deduplicated file paths."""

    def test_phase1_driver_returns_unique_files(self, search_engine, tmp_path):
        """Multiple matches in same file must collapse to a single Path entry."""
        (tmp_path / "multi.py").write_text("password = 1\npassword = 2\n")

        # Simulate two matches in the same file
        fake_matches = [
            _make_regex_match("multi.py", line_number=1, line_content="password = 1"),
            _make_regex_match("multi.py", line_number=2, line_content="password = 2"),
        ]
        fake_result = _make_search_result(fake_matches)

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(return_value=fake_result)

            candidates = search_engine._run_phase1_driver(
                tmp_path, "password", "content", [], []
            )

        # Only one Path for the file, even though two matches
        file_paths = [str(p) for p in candidates]
        assert len(set(file_paths)) == len(file_paths), (
            "duplicate paths must be removed"
        )
        assert len(candidates) == 1


class TestPhase1PositionsForIssue983:
    """Per-match positions must be accessible after _run_phase1_driver."""

    def test_phase1_positions_available_via_side_channel(self, search_engine, tmp_path):
        """After _run_phase1_driver, self._last_phase1_positions holds per-file matches."""
        (tmp_path / "c.py").write_text("password = 1\n")

        fake_match = _make_regex_match(
            "c.py", line_number=5, line_content="password = 1"
        )
        fake_result = _make_search_result([fake_match])

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(return_value=fake_result)

            search_engine._run_phase1_driver(tmp_path, "password", "content", [], [])

        assert hasattr(search_engine, "_last_phase1_positions"), (
            "_last_phase1_positions side-channel must exist for issue #983"
        )
        positions = search_engine._last_phase1_positions
        assert isinstance(positions, dict)
        # The file's path must be a key
        assert len(positions) == 1
        key = list(positions.keys())[0]
        assert isinstance(key, Path)
        # Each value is a list of (line_number, line_content) tuples
        matches_for_file = positions[key]
        assert isinstance(matches_for_file, list)
        assert len(matches_for_file) == 1
        line_num, line_text = matches_for_file[0]
        assert line_num == 5
        assert "password" in line_text


class TestAsyncToSyncHelper:
    """_run_async_in_sync works both outside and inside an event loop."""

    def test_async_in_sync_works_outside_event_loop(self, search_engine):
        """_run_async_in_sync can be called from synchronous context."""
        from code_indexer.xray.search_engine import _run_async_in_sync

        async def _coro():
            return 42

        result = _run_async_in_sync(_coro())
        assert result == 42

    def test_async_in_sync_works_inside_event_loop(self, search_engine):
        """_run_async_in_sync does not raise RuntimeError when called within asyncio.run."""
        from code_indexer.xray.search_engine import _run_async_in_sync

        async def _coro():
            return 99

        async def _outer():
            # We're already inside an event loop here
            return _run_async_in_sync(_coro())

        result = asyncio.run(_outer())
        assert result == 99


class TestPhase1FilenameTargetRetainsInlineBehavior:
    """search_target='filename' does NOT use RegexSearchService (no filename mode)."""

    def test_filename_target_does_not_call_regex_search_service(
        self, search_engine, tmp_path
    ):
        """RegexSearchService must NOT be called when search_target='filename'."""
        (tmp_path / "password_utils.py").write_text("def foo(): pass\n")

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            candidates = search_engine._run_phase1_driver(
                tmp_path, "password", "filename", [], []
            )

        MockService.assert_not_called()
        assert len(candidates) == 1
        assert candidates[0].name == "password_utils.py"
