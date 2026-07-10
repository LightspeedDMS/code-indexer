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

        # Superset guarantee: the file that truly contains the string is always a
        # candidate (ripgrep does the exact match over the candidates).
        assert "auth.java" in mgr.query(trigrams("LSAuthenticator"))
        assert "a/other.py" in mgr.query(trigrams("authenticate"))
        # a file that clearly lacks the (rare) trigrams is excluded
        assert "readme.md" not in mgr.query(trigrams("LSAuthenticator"))

    def test_query_requires_all_trigrams(self, tmp_path):
        repo = _repo(tmp_path)
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["auth.java", "a/other.py", "readme.md"])
        # trigrams drawn from two different files -> no single file has them all
        combined = trigrams("LSAuthenticator") | trigrams("relevant")
        assert mgr.query(combined) == []

    def test_query_excludes_files_lacking_rare_trigrams(self, tmp_path):
        repo = _repo(tmp_path)
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["auth.java", "a/other.py", "readme.md"])
        # a distinctive string present in no file -> its rare trigrams post to
        # nothing, so no indexed file is a candidate
        assert mgr.query(trigrams("zqxjwkbvfp")) == []

    def test_empty_required_returns_none(self, tmp_path):
        repo = _repo(tmp_path)
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["auth.java"])
        assert mgr.query(set()) is None

    def test_large_files_are_always_candidates(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "big.txt").write_text("x" * (6 * 1024 * 1024))  # > 5MB -> unindexed
        (repo / "code.txt").write_text("class Widget")
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["big.txt", "code.txt"])

        # a query matching nothing indexed must STILL include the large (unindexed)
        # file, which ripgrep would still scan
        assert set(mgr.query(trigrams("zzzznotpresent"))) == {"big.txt"}
        # an indexed match plus the always-candidate large file
        assert set(mgr.query(trigrams("widget"))) == {"big.txt", "code.txt"}

    def test_binary_files_are_indexed_and_searchable(self, tmp_path):
        # A file with NUL bytes still holds searchable text (ripgrep matches it),
        # so it is trigram-indexed -- selectable by its content, prunable when
        # its trigrams are absent -- NOT an opaque always-candidate.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "bin.dat").write_bytes(b"\x00\x01class OddAuthenticator\x00\xff")
        (repo / "other.txt").write_text("nothing relevant to see")
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["bin.dat", "other.txt"])
        assert set(mgr.query(trigrams("OddAuthenticator"))) == {"bin.dat"}
        assert "bin.dat" not in mgr.query(trigrams("zzzznotpresent"))

    def test_bucket_mask_prunes_cross_line_scatter(self, tmp_path):
        # A file where the required trigrams appear only on DIFFERENT, far-apart
        # lines cannot contain a default (line-oriented) match, so the bucket-mask
        # AND drops it -- while a file with them on the SAME line is kept.
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "together.txt").write_text("public enum Color {}\n")
        apart = ["public class A"] + ["filler line"] * 200 + ["enum B"]
        (repo / "apart.txt").write_text("\n".join(apart))
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["together.txt", "apart.txt"])

        req = trigrams("public") | trigrams("enum")
        cands = set(mgr.query(req))
        assert "together.txt" in cands  # same line -> shared bucket -> kept
        assert "apart.txt" not in cands  # far apart -> disjoint buckets -> pruned

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
