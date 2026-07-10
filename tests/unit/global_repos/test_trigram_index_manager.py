"""Tests for TrigramIndexManager (build + query).

Correctness focus: query() must return a superset of files that could match --
files with all required trigrams, plus any file that could not be indexed.
"""

import shutil

import pytest

from code_indexer.global_repos.regex_trigram import trigrams
from code_indexer.global_repos.trigram_index_manager import TrigramIndexManager


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "a").mkdir(parents=True)
    (repo / "auth.java").write_text("public class LSAuthenticator {}")
    (repo / "a" / "other.py").write_text("def authenticate(): pass")
    (repo / "readme.md").write_text("nothing relevant here at all")
    return repo


def _mgr(tmp_path):
    return TrigramIndexManager(tmp_path / "idx")


class TestBuildQuery:
    def test_build_and_query_finds_containing_files(self, tmp_path):
        repo = _repo(tmp_path)
        mgr = _mgr(tmp_path)
        n = mgr.build(repo, file_list=["auth.java", "a/other.py", "readme.md"])
        assert n == 3
        assert mgr.exists()

        # "authenticator" trigrams -> only auth.java
        cands = mgr.query(trigrams("authenticator"))
        assert cands == ["auth.java"]

        # "authenticate" trigrams -> only other.py
        cands = mgr.query(trigrams("authenticate"))
        assert set(cands) == {"a/other.py"}

    def test_query_requires_all_trigrams(self, tmp_path):
        repo = _repo(tmp_path)
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["auth.java", "a/other.py", "readme.md"])
        # combine trigrams from two different files -> no single file has both
        combined = trigrams("authenticator") | trigrams("relevant")
        assert mgr.query(combined) == []

    def test_empty_required_returns_none(self, tmp_path):
        repo = _repo(tmp_path)
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["auth.java"])
        assert mgr.query(set()) is None

    def test_binary_and_large_files_are_always_candidates(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "bin.dat").write_bytes(b"\x00\x01\x02binaryauthenticator\x00")
        (repo / "big.txt").write_text("x" * (6 * 1024 * 1024))  # > 5MB
        (repo / "code.txt").write_text("class Widget")
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["bin.dat", "big.txt", "code.txt"])

        # a query that matches none of the indexed content must STILL include the
        # un-indexed binary + large files (they might contain a match)
        cands = mgr.query(trigrams("zzzznotpresent"))
        assert set(cands) == {"bin.dat", "big.txt"}

        # an indexed file with the trigrams is added on top
        cands = mgr.query(trigrams("widget"))
        assert set(cands) == {"bin.dat", "big.txt", "code.txt"}

    def test_exists_false_without_build(self, tmp_path):
        assert _mgr(tmp_path).exists() is False

    def test_rebuild_is_atomic_and_replaces(self, tmp_path):
        repo = _repo(tmp_path)
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["auth.java"])
        assert mgr.query(trigrams("authenticator")) == ["auth.java"]
        # rebuild with a different file set
        mgr.build(repo, file_list=["readme.md"])
        assert mgr.query(trigrams("authenticator")) == []
        assert set(mgr.query(trigrams("relevant"))) == {"readme.md"}


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
class TestRgEnumeration:
    def test_build_via_rg_files(self, tmp_path):
        repo = _repo(tmp_path)
        # .ignore is honored by ripgrep regardless of git (a real golden repo is
        # a git repo where .gitignore applies identically for build + search).
        (repo / ".ignore").write_text("ignored/\n")
        (repo / "ignored").mkdir()
        (repo / "ignored" / "secret.java").write_text("class LSAuthenticator")
        mgr = _mgr(tmp_path)
        mgr.build(repo)  # no file_list -> rg --files enumeration
        # authenticator appears in auth.java (tracked) and ignored/secret.java
        # (ignored). rg --files excludes the ignored one.
        cands = mgr.query(trigrams("authenticator"))
        assert "auth.java" in cands
        assert "ignored/secret.java" not in cands
