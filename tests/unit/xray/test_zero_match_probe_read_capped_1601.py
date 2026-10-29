# ^ why rg hidden in the fixture; the RegexSearchService probe runs only without rg, #2306
from __future__ import annotations

import shutil
from unittest.mock import AsyncMock, patch

import pytest


def _make_search_result(matches, read_capped: bool = False):
    from code_indexer.global_repos.regex_search import RegexSearchResult

    return RegexSearchResult(
        matches=matches,
        total_matches=len(matches),
        truncated=False,
        search_engine="grep",
        search_time_ms=0.0,
        read_capped=read_capped,
    )


@pytest.fixture
def search_engine(tmp_path, monkeypatch):
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    real_which = shutil.which
    monkeypatch.setattr(
        "code_indexer.xray.search_engine.shutil.which",
        lambda cmd, *a, **kw: None if cmd == "rg" else real_which(cmd, *a, **kw),
    )
    return XRaySearchEngine()


def _probe(search_engine, tmp_path, result):
    with patch("code_indexer.xray.search_engine.RegexSearchService") as MockService:
        MockService.return_value.search = AsyncMock(return_value=result)
        return search_engine._probe_zero_match_patterns_content(tmp_path, ["**/*.py"])


class TestZeroMatchProbeReadCapped:
    def test_read_capped_probe_with_a_match_emits_no_warning(
        self, search_engine, tmp_path
    ):
        from code_indexer.global_repos.regex_search import RegexMatch

        match = RegexMatch(file_path="a.py", line_number=1, column=1, line_content="x")

        warnings = _probe(
            search_engine, tmp_path, _make_search_result([match], read_capped=True)
        )

        assert warnings == [], warnings

    def test_read_capped_probe_without_a_match_is_inconclusive(
        self, search_engine, tmp_path
    ):
        warnings = _probe(
            search_engine, tmp_path, _make_search_result([], read_capped=True)
        )

        assert [(w["type"], w["pattern"]) for w in warnings] == [
            ("zero_match_probe_failed", "**/*.py")
        ], warnings
