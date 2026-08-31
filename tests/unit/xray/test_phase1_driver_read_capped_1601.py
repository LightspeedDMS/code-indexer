"""Unit tests for Issue #1601 AC-E -- xray_search's content-mode phase-1
driver must surface RegexSearchResult.read_capped, not silently drop it.

Mirrors the fixture pattern established in
test_phase1_driver_regex_service.py (Bug #982).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


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
def search_engine(tmp_path):
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


class TestPhase1ContentDriverReadCappedSurfacing:
    """AC-E: the content-mode phase-1 driver must expose read_capped via
    the warnings side-channel, not silently drop it."""

    @pytest.mark.parametrize("read_capped_value", [True, False])
    def test_read_capped_warning_presence_matches_result(
        self, search_engine, tmp_path, read_capped_value
    ):
        """A read_capped warning appears iff RegexSearchResult.read_capped
        is True; it must never appear when False."""
        (tmp_path / "a.py").write_text("password = 1\n")
        fake_match = _make_regex_match("a.py", line_content="password = 1")
        fake_result = _make_search_result([fake_match], read_capped=read_capped_value)

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(return_value=fake_result)

            search_engine._run_phase1_driver(tmp_path, "password", "content", [], [])

        warnings = search_engine._last_phase1_warnings
        has_warning = any(
            w.get("type") == "content_search_read_capped" for w in warnings
        )
        assert has_warning is read_capped_value, (
            f"read_capped={read_capped_value} but warning presence was "
            f"{has_warning}; warnings={warnings}"
        )
