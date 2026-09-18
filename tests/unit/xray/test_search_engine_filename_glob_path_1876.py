"""Bug #1876 follow-up: `XRaySearchEngine._run_phase1_filename` (the
real, reachable filename-target-mode Phase 1 candidate walk) filters
include/exclude patterns with raw `fnmatch.fnmatch` against the
repository-relative path -- the SAME fnmatch-vs-ripgrep-glob-semantics
divergence GAP1 fixed in `regex_search.py` and `xray_graph.py`, just in
a third real call site. `**/*.md` silently fails to match a top-level
file; a bare directory name does not exclude its contents.

`_check_zero_match_patterns` (the zero-match advisory-warning helper
consumed right after this same walk) uses the identical raw fnmatch, so
it must be fixed in lockstep or its warnings will disagree with the
real (now-corrected) filtering.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")


def _engine():
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "README.md").write_text("top level\n")
    (repo / "docs" / "guide.md").write_text("nested\n")
    (repo / "notes.txt").write_text("not markdown\n")
    return repo


def test_filename_mode_include_pattern_matches_top_level_via_double_star(
    tmp_path,
):
    """`**/*.md` must match the TOP-LEVEL README.md, not just nested
    files -- raw fnmatch('README.md', '**/*.md') is False."""
    repo = _build_repo(tmp_path)
    engine = _engine()

    candidates = engine._run_phase1_filename(
        repo_path=repo,
        driver_regex=r".*",
        include_patterns=["**/*.md"],
        exclude_patterns=[],
    )

    rel_paths = {str(p.relative_to(repo)) for p in candidates}
    assert rel_paths == {"README.md", "docs/guide.md"}, (
        f"include_patterns=['**/*.md'] on filename-mode returned {rel_paths}, "
        f"expected both README.md and docs/guide.md"
    )


def test_filename_mode_zero_match_warning_agrees_with_real_filter(tmp_path):
    """A pattern that genuinely matches under correct gitwildmatch
    semantics, but ONLY against a top-level file with no `/` in its
    path, must NOT be reported as a zero-match warning. Raw fnmatch
    requires a literal `/` to satisfy `**/`, so with only a top-level
    README.md present (no nested .md files to coincidentally satisfy
    fnmatch too), the old buggy helper reports a false zero-match
    warning while the real filter correctly finds README.md.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("top level only\n")
    engine = _engine()

    candidates = engine._run_phase1_filename(
        repo_path=repo,
        driver_regex=r".*",
        include_patterns=["**/*.md"],
        exclude_patterns=[],
    )
    rel_paths = {str(p.relative_to(repo)) for p in candidates}
    assert rel_paths == {"README.md"}, (
        f"include_patterns=['**/*.md'] must match the top-level README.md, "
        f"got {rel_paths}"
    )

    warnings = engine._last_phase1_warnings
    warned_patterns = {w["pattern"] for w in warnings}
    assert "**/*.md" not in warned_patterns, (
        f"'**/*.md' genuinely matches README.md in this repo but was "
        f"reported as a zero-match warning: {warnings!r}"
    )


def test_zero_match_warning_scan_is_bounded_and_reports_incomplete_probe():
    """Bug #1876 item 5: an unmatched include pattern must not make the
    advisory warning path scan every file in a very large repository. Once
    its fixed match-attempt budget is exhausted, it must report an
    inconclusive probe instead of falsely claiming the pattern matched zero
    files.
    """
    engine = _engine()
    all_rel_paths = [f"generated/path_{index}.py" for index in range(10_001)]

    warnings = engine._check_zero_match_patterns(
        ["*.definitely_missing"], all_rel_paths
    )

    assert {warning["type"] for warning in warnings} == {"zero_match_probe_incomplete"}
    assert warnings[0]["patterns"] == ["*.definitely_missing"]


def test_zero_match_warning_uses_include_semantics_not_directory_containment_f9():
    """Bug #1876 round-5 finding F9 (AC of #1876): `_check_zero_match_
    patterns` compiled each probed pattern with the default
    `is_include=False` (gitignore/pathspec directory-containment
    semantics: a directory match implies its contents also match), NOT
    `is_include=True` (real ripgrep -g INCLUDE semantics, the same mode
    the actual filtering selector uses -- Bug #1876 N1). For
    `["src/*"]` over files `["src/a/b.ts"]`, the real selector picks
    NOTHING ("*" matches exactly one path segment, so "src/*" never
    reaches into "src/a/"), but the old default-mode probe reported no
    warning because "src/*" matched "src/a/b.ts" via the
    directory-containment optional group.
    """
    engine = _engine()
    warnings = engine._check_zero_match_patterns(["src/*"], ["src/a/b.ts"])

    assert {w["type"] for w in warnings} == {"zero_match_include_pattern"}, (
        f"'src/*' matches nothing under real INCLUDE semantics against "
        f"['src/a/b.ts'] and must be reported as a zero-match warning; "
        f"got {warnings!r}"
    )
    assert warnings[0]["pattern"] == "src/*"
