from __future__ import annotations

import logging
from pathlib import Path

import pytest

_SMALL_READ_CEILING = 4096


@pytest.fixture
def engine():
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for i in range(40):
        body = "".join(f"line {j} of file {i} padding\n" for j in range(20))
        (root / f"f{i:03d}.txt").write_text(body)
    (root / "needle.txt").write_text("the unique needle\n")
    (root / "empty.cfg").write_text("")
    return root


# ^ why lower the ceiling; a .* content probe passes 4096 bytes here, the narrow driver does not
@pytest.fixture
def small_ceiling(monkeypatch):
    monkeypatch.setattr(
        "code_indexer.global_repos.regex_search._MAX_READ_BYTES", _SMALL_READ_CEILING
    )


def _warnings(engine, repo: Path, include_patterns):
    engine._run_phase1_driver(repo, "unique needle", "content", include_patterns, [])
    return engine._last_phase1_warnings


def test_probe_with_matches_never_reads_to_the_ceiling(
    engine, repo, small_ceiling, caplog
):
    with caplog.at_level(logging.WARNING):
        warnings = _warnings(engine, repo, ["*.txt"])

    assert warnings == [], warnings
    assert not [r for r in caplog.records if "Output cap" in r.getMessage()]


def test_genuine_zero_match_pattern_still_warns(engine, repo, small_ceiling):
    warnings = _warnings(engine, repo, ["*.txt", "*.rs"])

    assert [(w["type"], w.get("pattern")) for w in warnings] == [
        ("zero_match_include_pattern", "*.rs")
    ], warnings


def test_pattern_matching_only_an_empty_file_is_not_zero_match(engine, repo):
    warnings = _warnings(engine, repo, ["*.cfg"])

    assert warnings == [], warnings


def test_probe_that_ripgrep_rejects_is_inconclusive(engine, repo):
    warnings = engine._probe_zero_match_patterns_content(repo, ["a["])

    assert [(w["type"], w["pattern"]) for w in warnings] == [
        ("zero_match_probe_failed", "a[")
    ], warnings
