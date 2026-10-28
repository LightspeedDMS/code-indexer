from pathlib import Path

import pytest

from perf_suite import corpus


def test_normalize_repo_url_canonicalizes_case_slash_git_suffix_and_ssh():
    expected = "github.com/pallets/click"
    assert corpus.normalize_repo_url("https://github.com/pallets/click") == expected
    assert corpus.normalize_repo_url("HTTPS://GitHub.com/Pallets/Click.git") == expected
    assert corpus.normalize_repo_url("https://github.com/pallets/click/") == expected
    assert corpus.normalize_repo_url("http://github.com/pallets/click.git/") == expected
    assert corpus.normalize_repo_url("git@github.com:pallets/click.git") == expected


def test_normalize_repo_url_keeps_distinct_repos_distinct():
    assert corpus.normalize_repo_url(
        "https://github.com/pallets/click"
    ) != corpus.normalize_repo_url("https://github.com/pallets/clicks")


def test_resolve_aliases_maps_urls_to_global_aliases_and_lists_missing():
    manifest_urls = [
        "https://github.com/pallets/click",
        "https://github.com/jhy/jsoup",
        "https://github.com/tqdm/tqdm",
    ]
    global_repos = [
        {
            "alias_name": "example-repo-global",
            "repo_url": "https://github.com/pallets/click.git",
        },
        {"alias_name": "jsoup-global", "repo_url": "https://GITHUB.com/jhy/jsoup/"},
        {
            "alias_name": "other-global",
            "repo_url": "https://example.com/other/repo.git",
        },
    ]
    resolved, missing = corpus.resolve_aliases(manifest_urls, global_repos)
    assert resolved == {
        "https://github.com/pallets/click": "example-repo-global",
        "https://github.com/jhy/jsoup": "jsoup-global",
    }
    assert missing == ["https://github.com/tqdm/tqdm"]


def test_resolve_aliases_reports_repo_as_missing_when_only_row_is_unusable():
    resolved, missing = corpus.resolve_aliases(
        ["https://github.com/pallets/click"],
        [{"repo_url": "https://github.com/pallets/click"}],
    )
    assert resolved == {}
    assert missing == ["https://github.com/pallets/click"]


def test_hit_found_matches_exact_path_or_path_suffix_only():
    hits = ["src/click/core.py", "tests/test_basic.py"]
    assert corpus.hit_found(hits, "src/click/core.py")
    assert corpus.hit_found(hits, "core.py")
    assert corpus.hit_found(hits, "click/core.py")
    assert not corpus.hit_found(hits, "ore.py")
    assert not corpus.hit_found(hits, "decorators.py")
    assert not corpus.hit_found([], "core.py")


def test_basename_expectation_fails_when_the_file_is_absent():
    assert corpus.hit_found(["pages/license.md"], "license.md")
    absent = ["pages/license.rst", "LICENSE.txt", "unlicense.md"]
    assert not corpus.hit_found(absent, "license.md")


def test_bundled_corpus_loads_and_names_the_eight_repositories():
    loaded = corpus.load_corpus(corpus.DEFAULT_CORPUS_PATH)
    names = sorted(corpus.normalize_repo_url(r.url) for r in loaded.repos)
    assert names == sorted(
        [
            "github.com/pallets/click",
            "github.com/tiangolo/typer",
            "github.com/tqdm/tqdm",
            "github.com/pallets/flask",
            "github.com/pallets/markupsafe",
            "github.com/jhy/jsoup",
            "github.com/microsoft/typescript",
            "github.com/homebrew/homebrew-core",
        ]
    )
    for repo in loaded.repos:
        assert repo.probe is not None, repo.url


def test_load_corpus_fails_loudly_on_unknown_mode(tmp_path: Path):
    bad = tmp_path / "corpus.yaml"
    bad.write_text(
        "top_n: 5\n"
        "repos:\n"
        "  - url: https://example.com/a/b\n"
        "    probe: {query: x}\n"
        "    queries:\n"
        "      - {mode: telepathy, query: x, expect: a.py}\n"
    )
    with pytest.raises(corpus.CorpusError, match="telepathy"):
        corpus.load_corpus(bad)
