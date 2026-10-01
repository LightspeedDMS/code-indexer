"""Discriminating RED tests for Bug #1876: include_patterns/exclude_patterns
are silently ignored by RegexSearchService.search() once a trigram index is
present.

Root cause (proven by direct code trace, not guessed): when a trigram index
exists, RegexSearchService._prefilter_candidate_files() returns a concrete
list of candidate files, and _search_ripgrep() then invokes ripgrep as
``rg --json -e pattern [-g inc] [-g !exc] -- file1 file2 file3 ...``.
Ripgrep's ``-g``/``--glob`` flags only filter its OWN recursive directory
walk -- they do not apply to files listed explicitly after ``--``. So the
moment the trigram pre-filter engages (true for any indexed global repo,
matching every production repro in issue #1876), every -g flag becomes a
silent no-op and include/exclude patterns are dropped on the floor.

These tests build a real repo, build a REAL trigram index over it (so the
buggy candidate-file code path actually engages -- a full, unindexed scan
already works correctly and would not catch this regression), and run
real ripgrep via RegexSearchService.search(). They assert on which FILES
come back, per the mission's requirement, not just counts.
"""

import shutil

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService
from code_indexer.global_repos.trigram_index_manager import TrigramIndexManager

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None, reason="ripgrep required for regex search"
)


@pytest.fixture(autouse=True)
def _no_lazy_build(monkeypatch):
    # Mirrors test_regex_search_trigram_prefilter.py: these tests build the
    # index explicitly and must not race the background lazy rebuild.
    monkeypatch.setenv("CIDX_TRIGRAM_LAZY_BUILD", "0")


def _build_repo(tmp_path):
    """A repo with the SAME literal ("FooAuthenticator") present in three
    different file extensions, mirroring the issue's exact repro shape
    (a .ts/.java hit leaking in in place of the requested .md, plus
    package-lock.json leaking into an extension-scoped include).
    """
    repo = tmp_path / "repo"
    (repo / "src" / "auth").mkdir(parents=True)
    (repo / "src" / "auth" / "Service.java").write_text(
        "public class FooAuthenticator {\n  void login() {}\n}\n"
    )
    (repo / "README.md").write_text("See FooAuthenticator for details.\n")
    (repo / "package-lock.json").write_text('{"name": "FooAuthenticator"}\n')
    (repo / "notes.txt").write_text("unrelated prose about cats\n")
    return repo


def _index(repo):
    mgr = TrigramIndexManager(repo / ".code-indexer" / "trigram_index")
    mgr.build(repo)
    return mgr


async def test_include_patterns_honoured_with_trigram_index_active(tmp_path):
    """Bug #1876 Defect 1, repro 1: include_patterns=["*.md"] must return
    ONLY the .md file, never the .java/.json files that also contain the
    literal. Currently (pre-fix) this returns ALL THREE files because the
    trigram pre-filter's candidate-file list bypasses ripgrep's -g flags
    entirely.
    """
    repo = _build_repo(tmp_path)
    _index(repo)
    assert (repo / ".code-indexer" / "trigram_index").exists()

    svc = RegexSearchService(repo)
    result = await svc.search(
        "FooAuthenticator", include_patterns=["*.md"], max_results=1000
    )
    files = {m.file_path for m in result.matches}
    assert files == {"README.md"}, (
        f"include_patterns=['*.md'] must return only README.md, got {files} "
        "-- ripgrep -g flags are being silently dropped for the "
        "trigram-prefiltered candidate-file list"
    )


async def test_exclude_patterns_honoured_with_trigram_index_active(tmp_path):
    """Bug #1876 Defect 1, repro 2: exclude_patterns=["*.java"] must remove
    the .java file from results. Currently (pre-fix) the .java file is
    still returned because -g '!*.java' is a no-op against explicit file
    args.
    """
    repo = _build_repo(tmp_path)
    _index(repo)

    svc = RegexSearchService(repo)
    result = await svc.search(
        "FooAuthenticator", exclude_patterns=["*.java"], max_results=1000
    )
    files = {m.file_path for m in result.matches}
    assert "src/auth/Service.java" not in files, (
        f"exclude_patterns=['*.java'] must remove the .java file, got {files}"
    )
    assert files == {"README.md", "package-lock.json"}


async def test_include_patterns_ts_style_extension_does_not_leak_json(tmp_path):
    """Bug #1876 Defect 1, repro 3: include_patterns=["*.java"] must NOT
    also return package-lock.json (the exact leak reported against
    contact-widget-api-global with include_patterns=["*.ts"]).
    """
    repo = _build_repo(tmp_path)
    _index(repo)

    svc = RegexSearchService(repo)
    result = await svc.search(
        "FooAuthenticator", include_patterns=["*.java"], max_results=1000
    )
    files = {m.file_path for m in result.matches}
    assert files == {"src/auth/Service.java"}, (
        f"include_patterns=['*.java'] leaked non-matching files: {files}"
    )


async def test_include_patterns_double_star_form_matches_nested_file(tmp_path):
    """Bug #1876 AC1: '**/*.ext' must behave consistently with '*.ext' for a
    file that is nested under a subdirectory.
    """
    repo = _build_repo(tmp_path)
    _index(repo)

    svc = RegexSearchService(repo)
    result = await svc.search(
        "FooAuthenticator", include_patterns=["**/*.java"], max_results=1000
    )
    files = {m.file_path for m in result.matches}
    assert files == {"src/auth/Service.java"}, (
        f"include_patterns=['**/*.java'] must match the nested .java file, got {files}"
    )


async def test_include_and_exclude_combined_with_trigram_index_active(tmp_path):
    """Bug #1876 AC1: include+exclude together must intersect correctly."""
    repo = _build_repo(tmp_path)
    (repo / "src" / "auth" / "OtherService.md").write_text(
        "Also mentions FooAuthenticator here.\n"
    )
    _index(repo)

    svc = RegexSearchService(repo)
    result = await svc.search(
        "FooAuthenticator",
        include_patterns=["*.md"],
        exclude_patterns=["**/auth/**"],
        max_results=1000,
    )
    files = {m.file_path for m in result.matches}
    assert files == {"README.md"}, (
        f"include+exclude combination must intersect correctly, got {files}"
    )


async def test_include_pattern_matching_nothing_returns_zero_with_trigram_index(
    tmp_path,
):
    """An include pattern that matches no real file must yield zero results
    even when the trigram pre-filter is active -- currently the -g no-op
    means the caller instead gets back the UNFILTERED candidate set.
    """
    repo = _build_repo(tmp_path)
    _index(repo)

    svc = RegexSearchService(repo)
    result = await svc.search(
        "FooAuthenticator",
        include_patterns=["*.zzznonexistentext"],
        max_results=1000,
    )
    assert result.matches == []
    assert result.total_matches == 0
