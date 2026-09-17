"""Bug #1876 N5: XRaySearchEngine.run() must validate
include_patterns/exclude_patterns ONCE in the service layer, exactly like
RegexSearchService.search(), so every entry point (MCP xray_search,
xray_explore, REST /api/xray/search, CLI) gets identical validation and
identical structured errors instead of relying on each MCP handler to
remember to call PathPatternMatcher.compile_patterns() itself.

Before this fix, `run()` performed zero pattern validation of its own --
a malformed or negated pattern reached Phase 1 candidate collection
unchecked.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

from code_indexer.services.path_pattern_matcher import InvalidPatternError

_NOOP_EVALUATOR = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"


def _engine():
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


def _build_repo(tmp_path):
    (tmp_path / "README.md").write_text("N5Needle\n")
    return tmp_path


def test_run_rejects_negation_include_pattern(tmp_path):
    repo = _build_repo(tmp_path)
    engine = _engine()

    with pytest.raises(InvalidPatternError):
        engine.run(
            repo_path=repo,
            driver_regex="N5Needle",
            evaluator_code=_NOOP_EVALUATOR,
            search_target="content",
            include_patterns=["!*.md"],
            timeout_seconds=30,
        )


def test_run_rejects_negation_exclude_pattern(tmp_path):
    repo = _build_repo(tmp_path)
    engine = _engine()

    with pytest.raises(InvalidPatternError):
        engine.run(
            repo_path=repo,
            driver_regex="N5Needle",
            evaluator_code=_NOOP_EVALUATOR,
            search_target="content",
            exclude_patterns=["!*.md"],
            timeout_seconds=30,
        )


def test_run_rejects_non_string_include_pattern_item(tmp_path):
    repo = _build_repo(tmp_path)
    engine = _engine()

    with pytest.raises(InvalidPatternError):
        engine.run(
            repo_path=repo,
            driver_regex="N5Needle",
            evaluator_code=_NOOP_EVALUATOR,
            search_target="content",
            include_patterns=[None],
            timeout_seconds=30,
        )


def test_run_valid_patterns_still_work(tmp_path):
    """Sanity: the new validation must not reject legitimate patterns or
    a normal successful run."""
    repo = _build_repo(tmp_path)
    engine = _engine()

    result = engine.run(
        repo_path=repo,
        driver_regex="N5Needle",
        evaluator_code=_NOOP_EVALUATOR,
        search_target="content",
        include_patterns=["*.md"],
        timeout_seconds=30,
    )
    assert result["files_total"] >= 1
