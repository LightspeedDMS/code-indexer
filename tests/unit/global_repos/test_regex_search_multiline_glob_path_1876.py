"""Bug #1876 follow-up: `_search_python_multiline` (the grep-engine
multiline fallback, reached only when the grep engine handles `multiline=True`)
matches include/exclude glob patterns against the bare FILENAME
(`fname`) instead of the repository-relative PATH (`rel_path`), even
though `rel_path` is already computed a few lines above the check.

This is strictly worse than the original #1876 defect: it is not just a
fnmatch-vs-ripgrep semantics mismatch, it means any path-segmented
pattern (`docs/**`, `src/*.ts`) can NEVER match anything, because a bare
filename like `guide.md` never contains a `/`. Proven here with a
pattern that must match a nested file and must NOT match a top-level
file of the same basename.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService

_MAX_RESULTS = 100


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "guide.md").write_text("MultilineNeedle\nsecond line\n")
    (repo / "guide.md").write_text("MultilineNeedle\nsecond line\n")
    return repo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_patterns", "exclude_patterns", "expected_files"),
    [
        (["docs/**"], None, {"docs/guide.md"}),
        (None, ["docs/**"], {"guide.md"}),
    ],
)
async def test_multiline_engine_respects_directory_segment(
    tmp_path, include_patterns, exclude_patterns, expected_files
):
    """`docs/**` must distinguish `docs/guide.md` from the top-level
    `guide.md` of the same basename -- bare-filename fnmatch cannot tell
    these two files apart at all."""
    repo = _build_repo(tmp_path)
    svc = RegexSearchService(repo)

    matches, _ = svc._search_python_multiline(
        pattern="MultilineNeedle.*second line",
        search_path=repo,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        case_sensitive=True,
        max_results=_MAX_RESULTS,
    )

    files = {m.file_path for m in matches}
    assert files == expected_files, (
        f"include_patterns={include_patterns} exclude_patterns="
        f"{exclude_patterns} on the multiline engine returned {files}, "
        f"expected {expected_files} -- bare-filename matching cannot "
        f"honour a directory-segmented pattern"
    )
