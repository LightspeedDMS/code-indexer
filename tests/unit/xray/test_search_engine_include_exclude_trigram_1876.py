"""Bug #1876 AC2/AC3: xray_search content-mode candidate collection must
honour include_patterns/exclude_patterns, and emit the documented
zero-match-include warning, when a trigram index is present.

`TestXRaySearchEngineIncludeExcludePatterns` in test_search_engine.py
covers the SAME include/exclude behavior but never builds a trigram
index -- every one of its repos hits RegexSearchService's full-scan
path, which was never affected by Bug #1876 (only the trigram-prefiltered
candidate-file path was). That is exactly why this shared-root-cause
defect shipped through xray_search undetected: `_run_phase1_content`
(search_engine.py) delegates straight to `RegexSearchService.search()`,
so it silently inherited the SAME `-g`-flags-ignored-for-explicit-file-
args bug Defect 1 fixed in `regex_search.py`.

These tests call `XRaySearchEngine._run_phase1_driver` directly (the
real Phase 1 candidate-selection method) rather than the full `.run()`
pipeline, so they exercise the real, unmocked candidate-filtering and
warning logic without requiring the Rust Phase 2 evaluator backend to
be built -- AC2/AC3 are purely Phase 1 concerns.
"""

import shutil
from pathlib import Path

import pytest

from code_indexer.global_repos.trigram_index_manager import TrigramIndexManager

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None, reason="ripgrep required for regex search"
)


@pytest.fixture(autouse=True)
def _no_lazy_build(monkeypatch):
    monkeypatch.setenv("CIDX_TRIGRAM_LAZY_BUILD", "0")


@pytest.fixture
def search_engine():
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "auth").mkdir(parents=True)
    (repo / "src" / "auth" / "Service.java").write_text(
        "public class FooAuthenticator {}\n"
    )
    (repo / "README.md").write_text("See FooAuthenticator for details.\n")
    (repo / "package-lock.json").write_text('{"name": "FooAuthenticator"}\n')
    return repo


def _index(repo: Path) -> None:
    mgr = TrigramIndexManager(repo / ".code-indexer" / "trigram_index")
    mgr.build(repo)


def test_content_mode_include_patterns_honoured_with_trigram_index(
    search_engine, tmp_path
):
    """AC2: candidate collection must honour include_patterns even when the
    trigram pre-filter is active -- must return only README.md, never the
    .java/.json files that also contain the literal.
    """
    repo = _build_repo(tmp_path)
    _index(repo)
    assert (repo / ".code-indexer" / "trigram_index").exists()

    candidates = search_engine._run_phase1_driver(
        repo,
        "FooAuthenticator",
        "content",
        ["*.md"],
        [],
        timeout_seconds=30,
    )
    rel_paths = {str(c.relative_to(repo)) for c in candidates}
    assert rel_paths == {"README.md"}, (
        f"include_patterns=['*.md'] must return only README.md, got {rel_paths}"
    )


def test_content_mode_exclude_patterns_honoured_with_trigram_index(
    search_engine, tmp_path
):
    """AC2: exclude_patterns must remove matching candidates even when the
    trigram pre-filter is active.
    """
    repo = _build_repo(tmp_path)
    _index(repo)

    candidates = search_engine._run_phase1_driver(
        repo,
        "FooAuthenticator",
        "content",
        [],
        ["*.java"],
        timeout_seconds=30,
    )
    rel_paths = {str(c.relative_to(repo)) for c in candidates}
    assert "src/auth/Service.java" not in rel_paths, (
        f"exclude_patterns=['*.java'] must remove the .java file, got {rel_paths}"
    )
    assert rel_paths == {"README.md", "package-lock.json"}


def test_content_mode_zero_match_include_pattern_filters_candidates(
    search_engine, tmp_path
):
    """AC3: an unmatched include_pattern must not leak trigram candidates."""
    repo = _build_repo(tmp_path)
    _index(repo)

    candidates = search_engine._run_phase1_driver(
        repo,
        "FooAuthenticator",
        "content",
        ["*.zzznonexistentext"],
        [],
        timeout_seconds=30,
    )
    assert candidates == []


def test_content_mode_zero_match_include_pattern_emits_warning(search_engine, tmp_path):
    """AC3: the real ripgrep-backed probe reports an unmatched glob.

    This deliberately tests the probe independently of candidate selection:
    a regression that drops only the warning append must fail here rather than
    being masked by the candidate-filter assertion above.
    """
    repo = _build_repo(tmp_path)
    warnings = search_engine._probe_zero_match_patterns_content(
        repo,
        ["*.zzznonexistentext"],
        timeout_seconds=30,
    )

    assert len(warnings) == 1
    assert warnings[0]["type"] == "zero_match_include_pattern"
    assert warnings[0]["pattern"] == "*.zzznonexistentext"


def test_content_mode_zero_match_hint_matches_filename_mode_hint(
    search_engine, tmp_path
):
    """Bug #1876 item 9: the content-mode probe's zero-match hint must not
    diverge from the filename-mode hint for the SAME warning type
    (`zero_match_include_pattern`). Before this fix, the content-mode hint
    still said 'fnmatch-style globs use `*` for a single path segment...
    use `**/time.py` instead of `*/time.py`' -- a claim that is now false,
    since item 3's `*/` -> `**/` normalization already makes a leading
    `*/` behave like `**/` (any depth) on both paths.
    """
    repo = _build_repo(tmp_path)

    content_warnings = search_engine._probe_zero_match_patterns_content(
        repo, ["*.zzznonexistentext"], timeout_seconds=30
    )
    filename_warnings = search_engine._check_zero_match_patterns(
        ["*.zzznonexistentext"], ["README.md", "src/auth/Service.java"]
    )

    assert content_warnings[0]["hint"] == filename_warnings[0]["hint"], (
        "content-mode and filename-mode must share ONE hint for the same "
        f"warning type, got content={content_warnings[0]['hint']!r} "
        f"filename={filename_warnings[0]['hint']!r}"
    )
    assert "fnmatch" not in content_warnings[0]["hint"].lower()


def _build_parity_repo(tmp_path: Path) -> Path:
    """Repo shaped after the mission's own "reviewer's example": a bare
    directory (`docs`) and a brace-alternation glob (`*.{ts,md}`), both of
    which must resolve consistently between the trigram-indexed candidate
    path and the ripgrep-backed zero-match probe.
    """
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "api.md").write_text("FooAuthenticator lives here\n")
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.ts").write_text("// FooAuthenticator\n")
    (repo / "README.md").write_text("FooAuthenticator overview\n")
    return repo


class TestZeroMatchProbeSharesMatcherWithMainSearch:
    """Bug #1876 item 3 / turn-9 open question: the xray content-mode
    zero-match probe must use the SAME matcher as the main candidate
    search, on a genuinely trigram-indexed repo -- not merely by code
    inspection. A pattern that legitimately matches real files must
    produce those candidates AND must never also emit a false
    zero-match warning for that same pattern.
    """

    def test_bare_directory_include_pattern_matches_with_no_false_warning(
        self, search_engine, tmp_path
    ):
        """Bug #1876 item 1/3: a bare directory token (`docs`, no glob
        metacharacters, no `/`) must resolve via the pinned bare-token
        policy (`**/docs`, `**/docs/**`) on the indexed candidate path,
        and the probe (routed through the same `RegexSearchService`) must
        agree -- finding the same file and raising no warning."""
        repo = _build_parity_repo(tmp_path)
        _index(repo)

        candidates = search_engine._run_phase1_driver(
            repo, "FooAuthenticator", "content", ["docs"], [], timeout_seconds=30
        )
        rel_paths = {str(c.relative_to(repo)) for c in candidates}
        assert rel_paths == {"docs/api.md"}, (
            f"bare 'docs' must resolve to the nested file under docs/, got {rel_paths}"
        )

        warnings = search_engine._probe_zero_match_patterns_content(
            repo, ["docs"], timeout_seconds=30
        )
        assert warnings == [], (
            "the probe must agree with the candidate search that 'docs' "
            f"matched real files -- got a false warning: {warnings}"
        )

    def test_brace_glob_include_pattern_matches_with_no_false_warning(
        self, search_engine, tmp_path
    ):
        """Bug #1876 item 1: a brace-alternation glob (`*.{ts,md}`) must
        expand on the indexed candidate path (pathspec gitwildmatch alone
        treats `{}` literally and would silently match nothing), and the
        probe must agree -- finding the same files and raising no
        warning."""
        repo = _build_parity_repo(tmp_path)
        _index(repo)

        candidates = search_engine._run_phase1_driver(
            repo,
            "FooAuthenticator",
            "content",
            ["*.{ts,md}"],
            [],
            timeout_seconds=30,
        )
        rel_paths = {str(c.relative_to(repo)) for c in candidates}
        assert rel_paths == {"README.md", "docs/api.md", "src/main.ts"}, (
            f"'*.{{ts,md}}' must brace-expand and match all .ts/.md files, got {rel_paths}"
        )

        warnings = search_engine._probe_zero_match_patterns_content(
            repo, ["*.{ts,md}"], timeout_seconds=30
        )
        assert warnings == [], (
            "the probe must agree with the candidate search that "
            f"'*.{{ts,md}}' matched real files -- got a false warning: {warnings}"
        )
