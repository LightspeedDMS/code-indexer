"""Integration tests: trigram pre-filter must not change regex search results.

The pre-filter is an optimization; for every pattern the results with the
trigram index present must be IDENTICAL to a full working-tree scan. These tests
build a repo, capture the full-scan baseline, then assert the indexed run
matches it exactly (same files + lines).
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
    # These tests build the index explicitly; disable the background lazy rebuild
    # so it does not race the explicit build on the same repo.
    monkeypatch.setenv("CIDX_TRIGRAM_LAZY_BUILD", "0")


def _build_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src" / "auth").mkdir(parents=True)
    (repo / "src" / "auth" / "LSAuthenticator.java").write_text(
        "package auth;\npublic class LSAuthenticator {\n  void login() {}\n}\n"
    )
    (repo / "src" / "auth" / "TokenAuthenticator.java").write_text(
        "public class TokenAuthenticator extends Base {}\n"
    )
    (repo / "src" / "Widget.java").write_text(
        "public class Widget { int authenticateCount; }\n"
    )
    (repo / "README.md").write_text("This project has authentication and widgets.\n")
    (repo / "notes.txt").write_text("nothing to see, just prose about cats\n")
    return repo


def _index(repo):
    mgr = TrigramIndexManager(repo / ".code-indexer" / "trigram_index")
    mgr.build(repo)
    return mgr


def _key(result):
    return sorted((m.file_path, m.line_number) for m in result.matches)


@pytest.mark.parametrize(
    "pattern",
    [
        "Authenticator",  # literal, index-eligible
        r"class\s+\w*Authenticator",  # regex with required literals
        r"authenticate\w*",  # partial-token literal (token index would miss)
        "public class Widget",  # spaced literal
        "TokenAuthenticator|LSAuthenticator",  # alternation -> pre-filter bails
        "zzzznotpresentanywhere",  # no matches
        r"a.c",  # too short -> pre-filter bails, still correct
    ],
)
async def test_prefilter_matches_full_scan(tmp_path, pattern):
    repo = _build_repo(tmp_path)
    svc = RegexSearchService(repo)

    # baseline: no trigram index -> full scan
    assert not (repo / ".code-indexer" / "trigram_index").exists()
    baseline = await svc.search(pattern, max_results=1000)

    # with trigram index -> pre-filtered scan
    _index(repo)
    svc2 = RegexSearchService(repo)
    indexed = await svc2.search(pattern, max_results=1000)

    assert _key(indexed) == _key(baseline), (
        f"pre-filter changed results for {pattern!r}: "
        f"baseline={_key(baseline)} indexed={_key(indexed)}"
    )


async def test_prefilter_used_when_index_present(tmp_path):
    """Sanity: a selective literal returns the expected files via the index."""
    repo = _build_repo(tmp_path)
    _index(repo)
    svc = RegexSearchService(repo)
    result = await svc.search("Authenticator", max_results=1000)
    files = {m.file_path for m in result.matches}
    assert files == {
        "src/auth/LSAuthenticator.java",
        "src/auth/TokenAuthenticator.java",
    }


async def test_non_ascii_match_not_dropped_by_prefilter(tmp_path):
    """Regression: a pattern with a non-ASCII literal must still find the file
    that genuinely contains it. The ASCII-only index formerly over-pruned because
    a required trigram spanning the non-ASCII char had zero document frequency."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "menu.py").write_text("label = 'café table'\n")
    (repo / "src" / "other.py").write_text("label = 'plain table'\n")
    _index(repo)
    svc = RegexSearchService(repo)
    result = await svc.search("café table", max_results=1000)
    assert {m.file_path for m in result.matches} == {"src/menu.py"}


async def test_multiline_cross_line_match_not_dropped_by_prefilter(tmp_path):
    """Regression: a genuine CROSS-LINE multiline match must not be dropped by
    the trigram pre-filter.

    A prior version of the index stored a per-line "bucket" bitmask per
    (trigram, file) and pruned a file when the required trigrams' masks did
    not share a bucket -- correct for single-line ripgrep matches, but wrong
    for a multiline pattern whose required literals legitimately live on
    different lines (see reverted commit "perf: positional line-bucket
    bitmask to prune cross-line trigram scatter"). The file-level (trigram ->
    file) intersection the index uses today has no such assumption: it must
    surface the file as a candidate whenever it contains each required
    trigram ANYWHERE, and ripgrep's own multiline pass then confirms the
    match.
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "cross.py").write_text(
        "foo_marker\nline2\nline3\nline4\nbar_marker\n"
    )
    _index(repo)
    svc = RegexSearchService(repo)
    result = await svc.search(
        r"foo_marker[\s\S]*bar_marker", multiline=True, max_results=1000
    )
    assert result.total_matches == 1
    assert {m.file_path for m in result.matches} == {"src/cross.py"}


async def test_binary_file_with_match_not_missed(tmp_path):
    """A match inside an un-indexed (binary) file must still be found."""
    repo = tmp_path / "repo"
    (repo).mkdir()
    # embed a text-y match inside a file with a NUL byte (treated as binary at
    # index time -> always-candidate). ripgrep with -a would search it; default
    # rg treats it as binary. Use a file that is text but has a high-byte to
    # exercise the latin-1 path instead, guaranteeing a real match.
    (repo / "weird.txt").write_bytes(
        "class OddAuthenticator\n".encode("latin-1") + b"\xff\n"
    )
    _index(repo)
    svc = RegexSearchService(repo)
    result = await svc.search("OddAuthenticator", max_results=1000)
    assert {m.file_path for m in result.matches} == {"weird.txt"}
