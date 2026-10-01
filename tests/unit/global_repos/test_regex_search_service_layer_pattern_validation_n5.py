"""Bug #1876 N5: pattern validation must happen ONCE in the service layer
(RegexSearchService.search) so every front door (MCP, REST, CLI) gets
identical validation and identical structured errors, instead of relying on
each front door to remember to call PathPatternMatcher.compile_patterns()
itself.

Concrete pre-fix defect (verified live): on an UNINDEXED repo (no trigram
index -- the plain ripgrep `-g` walk path), include_patterns=["!*.md"]
never reaches PathPatternMatcher at all. It is handed straight to ripgrep's
own `-g` flag, which interprets a leading "!" as ITS OWN negation syntax --
silently converting a caller's INCLUDE into an EXCLUDE with zero error, so
the search returns every NON-markdown file instead of failing loud. The
INDEXED path (trigram pre-filter engaged, candidate_files not None) already
raises InvalidPatternError via the selector built in _search_ripgrep, so the
two paths silently disagree on the exact same caller input depending on
whether a trigram index happens to exist.
"""

from __future__ import annotations

import shutil

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService
from code_indexer.services.path_pattern_matcher import InvalidPatternError

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None, reason="ripgrep required for regex search"
)


def _build_unindexed_repo(tmp_path):
    (tmp_path / "README.md").write_text("N5Needle\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("N5Needle\n")
    return tmp_path


@pytest.mark.asyncio
async def test_search_rejects_negation_include_pattern_on_unindexed_repo(tmp_path):
    """No trigram index -- the plain ripgrep -g walk path. Must raise
    InvalidPatternError, never silently invert the include into an
    exclude via ripgrep's own '!' negation syntax."""
    repo = _build_unindexed_repo(tmp_path)
    svc = RegexSearchService(repo)

    with pytest.raises(InvalidPatternError):
        await svc.search(
            "N5Needle", include_patterns=["!*.md"], max_results=100, timeout_seconds=30
        )


@pytest.mark.asyncio
async def test_search_rejects_negation_exclude_pattern_on_unindexed_repo(tmp_path):
    repo = _build_unindexed_repo(tmp_path)
    svc = RegexSearchService(repo)

    with pytest.raises(InvalidPatternError):
        await svc.search(
            "N5Needle", exclude_patterns=["!*.md"], max_results=100, timeout_seconds=30
        )


@pytest.mark.asyncio
async def test_search_rejects_non_string_include_pattern_item(tmp_path):
    """A non-string pattern item must fail loud at the service layer even
    for a direct programmatic caller that skips any front-door handler."""
    repo = _build_unindexed_repo(tmp_path)
    svc = RegexSearchService(repo)

    with pytest.raises(InvalidPatternError):
        await svc.search(
            "N5Needle",
            include_patterns=[None],  # type: ignore[list-item]
            max_results=100,
            timeout_seconds=30,
        )


@pytest.mark.asyncio
async def test_search_valid_patterns_still_work(tmp_path):
    """Sanity: the new validation must not reject legitimate patterns."""
    repo = _build_unindexed_repo(tmp_path)
    svc = RegexSearchService(repo)

    result = await svc.search(
        "N5Needle", include_patterns=["*.md"], max_results=100, timeout_seconds=30
    )
    assert {m.file_path for m in result.matches} == {"README.md"}
