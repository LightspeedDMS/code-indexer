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

    def test_exists_false_without_build(self, tmp_path):
        assert _mgr(tmp_path).exists() is False


class TestSchemaVersionGuard:
    """An index whose on-disk schema does not match this build must be reported
    as absent so the caller full-scans and a rebuild regenerates it -- instead of
    the query failing on a missing table/column."""

    def _built(self, tmp_path):
        repo = _repo(tmp_path)
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["auth.java"])
        return mgr

    def test_current_index_exists(self, tmp_path):
        assert self._built(tmp_path).exists() is True

    def test_wrong_schema_version_is_absent(self, tmp_path):
        import sqlite3

        mgr = self._built(tmp_path)
        with sqlite3.connect(mgr.db_path) as conn:
            conn.execute("UPDATE meta SET value = 999999 WHERE key = 'schema_version'")
        assert mgr.exists() is False  # version mismatch -> rebuild

    def test_missing_schema_stamp_is_absent(self, tmp_path):
        import sqlite3

        mgr = self._built(tmp_path)
        with sqlite3.connect(mgr.db_path) as conn:
            conn.execute("DELETE FROM meta WHERE key = 'schema_version'")
        assert mgr.exists() is False  # pre-stamp/old format -> rebuild

    def test_missing_table_is_absent(self, tmp_path):
        import sqlite3

        mgr = self._built(tmp_path)
        with sqlite3.connect(mgr.db_path) as conn:
            conn.execute("DROP TABLE meta")
        assert mgr.exists() is False  # missing table -> caught -> rebuild

    def test_rebuild_is_atomic_and_replaces(self, tmp_path):
        repo = _repo(tmp_path)
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["auth.java"])
        assert mgr.query(trigrams("authenticator")) == ["auth.java"]
        # rebuild with a different file set
        mgr.build(repo, file_list=["readme.md"])
        assert mgr.query(trigrams("authenticator")) == []
        assert set(mgr.query(trigrams("relevant"))) == {"readme.md"}


class TestBuildAtomicity:
    """A build publishes atomically and never leaves a partial index behind, so a
    concurrent build on a shared (NFS, cluster-mode) index dir cannot clobber
    another's in-progress database and publish a partially-populated index that
    would pass exists() and silently drop matches."""

    def test_failed_build_keeps_prior_index_and_leaves_no_temp(self, tmp_path):
        import code_indexer.global_repos.trigram_index_manager as tim

        repo = _repo(tmp_path)
        mgr = _mgr(tmp_path)
        mgr.build(repo, file_list=["auth.java"])
        assert mgr.query(trigrams("lsauthenticator")) == ["auth.java"]

        def boom(_abs_path):
            raise RuntimeError("disk full mid-build")

        orig = tim.TrigramIndexManager._file_trigrams
        tim.TrigramIndexManager._file_trigrams = staticmethod(boom)
        try:
            with pytest.raises(RuntimeError):
                mgr.build(repo, file_list=["a/other.py"])
        finally:
            tim.TrigramIndexManager._file_trigrams = staticmethod(orig)

        # the previously-published index is untouched (no partial replace)...
        assert mgr.query(trigrams("lsauthenticator")) == ["auth.java"]
        # ...and the failed build cleaned up its unique temp file.
        assert list((tmp_path / "idx").glob("*.db.building")) == []

    def test_concurrent_builds_publish_a_valid_index(self, tmp_path):
        import threading
        import time

        import code_indexer.global_repos.trigram_index_manager as tim

        repo = _repo(tmp_path)
        mgr = _mgr(tmp_path)

        # Slow the per-file work so the three builds genuinely overlap against the
        # same index dir (each must use its own temp file).
        real = tim.TrigramIndexManager._file_trigrams

        def slow(abs_path):
            time.sleep(0.02)
            return real(abs_path)

        tim.TrigramIndexManager._file_trigrams = staticmethod(slow)
        errors: list = []

        def run():
            try:
                mgr.build(repo, file_list=["auth.java", "a/other.py", "readme.md"])
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        try:
            threads = [threading.Thread(target=run) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            tim.TrigramIndexManager._file_trigrams = staticmethod(real)

        assert errors == []
        # the published index is complete and valid -- not a clobbered partial.
        assert mgr.exists()
        assert "auth.java" in mgr.query(trigrams("lsauthenticator"))
        assert "a/other.py" in mgr.query(trigrams("authenticate"))
        # no leftover in-progress temp files.
        assert list((tmp_path / "idx").glob("*.db.building")) == []


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
