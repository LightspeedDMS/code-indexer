"""Regression: _collect_files caches the full-tree walk per repo_path.

The walk (rglob + a stat + an is_indexed check per file) ran on every
list_files / browse request; on large repos it dominated latency. A short-TTL
per-repo_path cache makes repeated calls (a client paging a repo) reuse the
walk. The indexable-extensions lookup is also hoisted out of the per-file loop.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.services import file_service as fs_mod
from code_indexer.server.services.file_service import FileListingService


def _svc():
    # Skip __init__ (ActivatedRepoManager wiring) — we exercise _collect_files only.
    return FileListingService.__new__(FileListingService)


def _make_tree(root, n=5):
    for i in range(n):
        (Path(root) / f"f{i}.py").write_text("x")
    (Path(root) / "note.md").write_text("x")  # non-indexable extension


def _patch_config():
    cfg = MagicMock()
    cfg.indexing_config.indexable_extensions = [".py"]
    svc = MagicMock()
    svc.get_config.return_value = cfg
    return patch.object(fs_mod, "get_config_service", return_value=svc)


def test_second_call_hits_cache():
    with tempfile.TemporaryDirectory() as d:
        _make_tree(d)
        fs_mod._collect_cache.clear()
        svc = _svc()
        calls = {"n": 0}
        real = FileListingService._collect_files_uncached

        def counting(self, repo_path):
            calls["n"] += 1
            return real(self, repo_path)

        with (
            patch.object(FileListingService, "_collect_files_uncached", counting),
            _patch_config(),
        ):
            r1 = svc._collect_files(d)
            r2 = svc._collect_files(d)

        assert calls["n"] == 1, f"expected 1 walk (cache hit on 2nd), got {calls['n']}"
        assert r1 == r2


def test_is_indexed_hoist_matches_extensions():
    with tempfile.TemporaryDirectory() as d:
        _make_tree(d)
        fs_mod._collect_cache.clear()
        with _patch_config():
            files = _svc()._collect_files(d)
        by_name = {f.path: f.is_indexed for f in files}
        assert by_name["f0.py"] is True
        assert by_name["note.md"] is False


def test_distinct_repo_paths_do_not_collide():
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        _make_tree(d1, n=2)
        _make_tree(d2, n=4)
        fs_mod._collect_cache.clear()
        with _patch_config():
            r1 = _svc()._collect_files(d1)
            r2 = _svc()._collect_files(d2)
        assert len(r1) != len(r2)  # each path cached independently
