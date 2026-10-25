import logging
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService
from code_indexer.global_repos.regex_trigram import trigrams
from code_indexer.global_repos.trigram_index_manager import TrigramIndexManager

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None, reason="ripgrep required for trigram enumeration"
)

_BAD_NAME = os.fsdecode(b"bad\xff.py")
_LOGGER = "code_indexer.global_repos.trigram_index_manager"


@pytest.fixture(autouse=True)
def _no_lazy_build(monkeypatch):
    monkeypatch.setenv("CIDX_TRIGRAM_LAZY_BUILD", "0")


def _write_bad(directory: Path, content: bytes) -> None:
    with open(os.path.join(os.fsencode(directory), b"bad\xff.py"), "wb") as fh:
        fh.write(content)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("class LSAuthenticator:\n    pass\n")
    _write_bad(repo, b"class LSAuthenticator:\n    pass\n")
    return repo


def _mgr(repo: Path) -> TrigramIndexManager:
    return TrigramIndexManager(repo / ".code-indexer" / "trigram_index")


def _index(repo: Path) -> TrigramIndexManager:
    mgr = _mgr(repo)
    mgr.build(repo)
    return mgr


def _messages(caplog) -> list:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER]


def test_build_keeps_non_utf8_name_out_of_file_rows_and_searches_it_without_index(
    tmp_path, caplog
):
    repo = _repo(tmp_path)
    mgr = _mgr(repo)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        count = mgr.build(repo)

    # ^ invariant build() counts `files` rows; the bad name lives only in unstorable_names
    assert count == 1
    assert mgr.exists()
    assert mgr.query(trigrams("LSAuthenticator")) == [_BAD_NAME, "ok.py"]
    assert mgr.query(trigrams("zqxjwkbvfp")) == [_BAD_NAME]
    skip_lines = [m for m in _messages(caplog) if "bad\\xff.py" in m]
    assert len(skip_lines) == 1
    assert "not valid UTF-8" in skip_lines[0]
    assert "searched without the index" in skip_lines[0]
    for message in _messages(caplog):
        message.encode("utf-8")
        assert _BAD_NAME not in message


def test_lease_directory_is_not_indexed(tmp_path):
    repo = _repo(tmp_path)
    leases = repo / ".snapshot-reader-leases"
    leases.mkdir()
    (leases / "lease.json").write_text("LSAuthenticator lease")

    mgr = _index(repo)

    assert mgr.query(trigrams("LSAuthenticator")) == [_BAD_NAME, "ok.py"]


async def test_regex_search_uses_index_with_non_utf8_name_present(tmp_path):
    repo = _repo(tmp_path)
    _index(repo)
    svc = RegexSearchService(repo)

    candidates = svc._prefilter_candidate_files(
        "LSAuthenticator", repo, None, case_sensitive=True
    )
    result = await svc.search("LSAuthenticator", max_results=100)

    assert candidates == [(repo / _BAD_NAME).resolve(), (repo / "ok.py").resolve()]
    assert len(result.matches) == 2
    assert "ok.py" in {m.file_path for m in result.matches}


async def test_regex_only_match_in_non_utf8_name_found_through_index(tmp_path):
    repo = _repo(tmp_path)
    with open(os.path.join(os.fsencode(repo), b"bad\xff.py"), "ab") as fh:
        fh.write(b"needle_only_in_bad_name = 1\n")
    _index(repo)
    svc = RegexSearchService(repo)

    candidates = svc._prefilter_candidate_files(
        "needle_only_in_bad_name", repo, None, case_sensitive=True
    )
    result = await svc.search("needle_only_in_bad_name", max_results=100)

    assert candidates == [(repo / _BAD_NAME).resolve()]
    assert [m.line_content for m in result.matches] == ["needle_only_in_bad_name = 1"]


def test_skipped_name_respects_search_scope(tmp_path):
    repo = _repo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "sub" / "inner.py").write_text("LSAuthenticator()\n")
    _index(repo)
    svc = RegexSearchService(repo)

    candidates = svc._prefilter_candidate_files(
        "LSAuthenticator", repo / "sub", "sub", case_sensitive=True
    )

    assert candidates == [(repo / "sub" / "inner.py").resolve()]


def test_index_without_unstorable_table_reads_as_none(tmp_path):
    repo = _repo(tmp_path)
    mgr = _index(repo)
    with sqlite3.connect(str(mgr.db_path)) as conn:
        conn.execute("DROP TABLE unstorable_names")

    assert mgr.exists()
    assert mgr.query(trigrams("LSAuthenticator")) == ["ok.py"]


def test_enumerate_keeps_newline_in_name_and_drops_trailing_nul(tmp_path):
    repo = _repo(tmp_path)
    (repo / "two\nlines.py").write_text("x = 1\n")

    names = _mgr(repo)._enumerate_files(repo)

    assert sorted(names) == sorted([_BAD_NAME, "ok.py", "two\nlines.py"])


def test_enumerate_empty_repository_returns_empty_list(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()

    assert _mgr(repo)._enumerate_files(repo) == []
