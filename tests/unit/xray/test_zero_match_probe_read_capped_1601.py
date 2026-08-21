"""Unit test for Issue #1601 AC-E -- xray_search's zero-match-pattern probe
(``_probe_zero_match_patterns_content``) must surface read_capped too.

The issue calls this out as the highest-exposure code-confirmed caller:
its driver pattern is a bare ``.*`` (matches every line of every admitted
file), broader than the pattern class that empirically crashed a server
elsewhere in this issue.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


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


class TestZeroMatchProbeReadCappedSurfacing:
    """AC-E: the zero-match-pattern probe must not silently drop
    read_capped when its own .* probe call gets capped."""

    def test_read_capped_true_surfaces_a_warning(self, search_engine, tmp_path):
        from code_indexer.global_repos.regex_search import RegexMatch

        fake_match = RegexMatch(
            file_path="a.py", line_number=1, column=1, line_content="x"
        )
        fake_result = _make_search_result([fake_match], read_capped=True)

        with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
            instance = MockService.return_value
            instance.search = AsyncMock(return_value=fake_result)

            warnings = search_engine._probe_zero_match_patterns_content(
                tmp_path, ["**/*.py"]
            )

        assert any(w.get("type") == "content_search_read_capped" for w in warnings), (
            f"expected a read_capped warning, got: {warnings}"
        )
