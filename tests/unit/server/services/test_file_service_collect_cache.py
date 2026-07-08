"""Tests for the list_files/browse_directory bottleneck fix: _collect_files is
cached per repo_path so a client paging a large repo doesn't re-walk the whole
tree (per-file stat + config lookup) on every request."""

import pytest

from src.code_indexer.server.services import file_service as fs
from src.code_indexer.server.services.file_service import FileListingService


@pytest.fixture
def service():
    # Build without __init__ (no DB dependency), matching the other file_service tests.
    svc = FileListingService.__new__(FileListingService)
    svc.activated_repo_manager = None  # type: ignore[assignment]
    return svc


@pytest.fixture(autouse=True)
def _clear_cache():
    fs._collect_cache.clear()
    yield
    fs._collect_cache.clear()


def _make_repo(tmp_path):
    (tmp_path / "a.py").write_text("print(1)")
    (tmp_path / "b.py").write_text("print(2)")
    return str(tmp_path)


def test_second_call_hits_cache_no_rewalk(tmp_path, service, monkeypatch):
    repo = _make_repo(tmp_path)
    calls = {"n": 0}
    real = service._collect_files_uncached

    def spy(rp):
        calls["n"] += 1
        return real(rp)

    monkeypatch.setattr(service, "_collect_files_uncached", spy)

    f1 = service._collect_files(repo)
    f2 = service._collect_files(repo)

    assert calls["n"] == 1, "second call must reuse the cache, not re-walk the tree"
    assert f1 is f2, "cache returns the same collected list object"


def test_expired_entry_rewalks_and_sees_new_files(tmp_path, service):
    repo = _make_repo(tmp_path)
    service._collect_files(repo)
    # Force-expire the cached entry.
    _exp, files = fs._collect_cache[repo]
    fs._collect_cache[repo] = (0.0, files)

    (tmp_path / "c.py").write_text("print(3)")  # appears only after a re-walk
    paths = {f.path for f in service._collect_files(repo)}
    assert "c.py" in paths, "an expired entry must re-walk and pick up new files"


def test_distinct_repo_paths_are_separate_entries(tmp_path, service):
    r1 = tmp_path / "r1"
    r1.mkdir()
    (r1 / "x.py").write_text("x")
    r2 = tmp_path / "r2"
    r2.mkdir()
    (r2 / "y.py").write_text("y")

    service._collect_files(str(r1))
    service._collect_files(str(r2))
    assert str(r1) in fs._collect_cache
    assert str(r2) in fs._collect_cache


def test_cache_is_lru_bounded(tmp_path, service):
    for i in range(fs._COLLECT_CACHE_MAX_REPOS + 5):
        d = tmp_path / f"r{i}"
        d.mkdir()
        (d / "f.py").write_text("x")
        service._collect_files(str(d))
    assert len(fs._collect_cache) <= fs._COLLECT_CACHE_MAX_REPOS
