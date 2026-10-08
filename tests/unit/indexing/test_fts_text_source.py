"""Full-text indexing reads file text through the content backend when delegated.

Pinned, with the mutation each test exists to catch:

* The text is EXACTLY what a text-mode ``open`` produces -- universal newlines,
  BOM kept, invalid bytes per the caller's ``errors`` -- because a delegated
  build must store the documents a local one stores.
* Batched: one fetch per planned batch, never one per file; and refused outright
  without a manifest, where batching would silently degrade to per-file.
* No disk: proven against a codebase path that does not exist.
* End to end: the real ``cidx index --rebuild-fts-index`` the server's FTS
  backfill spawns, run as a subprocess with a real plugin resolved through
  ``CIDX_CONTENT_BACKEND``, against a project directory holding NO source
  files -- so every document indexed came through the backend.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence
from unittest.mock import MagicMock

import pytest

from code_indexer.indexing.file_texts import decode_like_text_mode, iter_file_texts
from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import (
    FetchedFile,
    LocalContentBackend,
)

SHAPES: Dict[str, bytes] = {
    "lf.py": b"def a():\n    return 1\n",
    "crlf.py": b"def a():\r\n    return 1\r\n",
    "cr.py": b"def a():\r    return 1\r",
    "bom.py": "﻿def a():\n    pass\n".encode("utf-8"),
    "invalid.py": b"x = '\xff\xfe'\n",
    "unicode.py": "s = 'café \U0001f600'\n".encode("utf-8"),
    "empty.py": b"",
    "nested/mod.py": b"import os\n",
}


class _Delegated:
    """Serves ``source`` whatever root it is asked about; records every fetch."""

    is_local = False

    def __init__(self, source: Path) -> None:
        self._source = source
        self._inner = LocalContentBackend()
        self.calls: List[List[str]] = []

    def manifest(self, repo_path: Any, **k: Any) -> Any:
        return self._inner.manifest(self._source, **k)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        self.calls.append(list(paths))
        return self._inner.fetch(self._source, paths)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    for rel, data in SHAPES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


@pytest.mark.parametrize("errors", ["replace", "ignore"])
def test_decoding_is_exactly_a_text_mode_read(source: Path, errors: str) -> None:
    for rel, raw in SHAPES.items():
        with open(source / rel, "r", encoding="utf-8", errors=errors) as f:
            expected = f.read()
        assert decode_like_text_mode(raw, errors) == expected, rel


@pytest.mark.parametrize("errors", ["replace", "ignore"])
def test_delegated_texts_equal_local_texts_with_no_disk(
    source: Path, tmp_path: Path, errors: str
) -> None:
    absent = tmp_path / "not-mounted"
    backend = _Delegated(source)
    manifest = RepoManifest.build(absent, backend=backend)
    files = [absent / rel for rel in SHAPES]

    delegated = {
        p.relative_to(absent).as_posix(): t
        for p, t, _ in iter_file_texts(
            files, absent, errors=errors, manifest=manifest, backend=backend
        )
    }
    local = {
        p.relative_to(source).as_posix(): t
        for p, t, _ in iter_file_texts(
            [source / rel for rel in SHAPES], source, errors=errors
        )
    }

    assert delegated == local
    assert len(backend.calls) == 1, "a small repo is one batch, not one per file"


def test_a_backend_without_a_manifest_is_refused(source: Path) -> None:
    """Without sizes every file would travel alone: per-file, quietly."""
    with pytest.raises(ValueError, match="manifest"):
        list(
            iter_file_texts(
                [source / "lf.py"], source, errors="replace", backend=_Delegated(source)
            )
        )


def test_a_per_file_error_is_yielded_not_raised(source: Path) -> None:
    class _Denied(_Delegated):
        def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[Any]:
            for p in paths:
                yield FetchedFile(path=p, blob_sha=None, data=b"", error="EACCES")

    backend = _Denied(source)
    manifest = RepoManifest.build(source, backend=backend)
    (result,) = iter_file_texts(
        [source / "lf.py"], source, errors="replace", manifest=manifest, backend=backend
    )
    assert result.text is None and result.error == "EACCES"


def test_a_transport_failure_fails_its_batch_without_raising(source: Path) -> None:
    class _Down(_Delegated):
        def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[Any]:
            raise ConnectionError("reset")

    backend = _Down(source)
    manifest = RepoManifest.build(source, backend=backend)
    results = list(
        iter_file_texts(
            [source / "lf.py", source / "cr.py"],
            source,
            errors="replace",
            manifest=manifest,
            backend=backend,
        )
    )
    assert [r.text for r in results] == [None, None]
    assert all("content fetch failed" in (r.error or "") for r in results)


def test_smart_indexers_fts_fill_reads_through_the_backend(
    source: Path, tmp_path: Path
) -> None:
    from code_indexer.config import Config
    from code_indexer.indexing.file_finder import FileFinder
    from code_indexer.services.smart_indexer import SmartIndexer

    absent = tmp_path / "not-mounted"
    backend = _Delegated(source)
    config = Config(codebase_dir=absent, file_extensions=["py"])
    indexer = SmartIndexer.__new__(SmartIndexer)
    indexer.config = config
    indexer.content_backend = backend
    indexer.repo_manifest = RepoManifest.build(absent, backend=backend)
    indexer.file_finder = FileFinder(config)
    indexer.file_finder.set_manifest(indexer.repo_manifest, backend)
    fts = MagicMock()

    indexer._populate_fts_from_all_files(fts)

    docs = {
        c.args[0]["path"]: c.args[0]["content"] for c in fts.add_document.call_args_list
    }
    assert docs["crlf.py"] == "def a():\n    return 1\n"
    assert docs["nested/mod.py"] == "import os\n"
    assert set(docs) == set(SHAPES)


# ── end to end: the command the server's FTS backfill spawns ───────────────

_PLUGIN = textwrap.dedent(
    """
    import os
    from code_indexer.server.storage.shared.content_backend import LocalContentBackend

    class _ElsewhereBackend:
        is_local = False

        def __init__(self):
            self._source = os.environ["TEST_CONTENT_SOURCE"]
            self._inner = LocalContentBackend()

        def manifest(self, repo_path, **kw):
            return self._inner.manifest(self._source, **kw)

        def fetch(self, repo_path, paths):
            return self._inner.fetch(self._source, paths)

    def make():
        return _ElsewhereBackend()
    """
)


def test_the_backfill_command_indexes_files_that_are_not_on_this_disk(
    source: Path, tmp_path: Path
) -> None:
    src = Path(__file__).resolve().parents[3] / "src"
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "fts_elsewhere_plugin.py").write_text(_PLUGIN)
    project = tmp_path / "project"  # holds config only -- no source files
    project.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(src), str(plugin_dir)]),
        "CIDX_CONTENT_BACKEND": "fts_elsewhere_plugin:make",
        "TEST_CONTENT_SOURCE": str(source),
    }

    def cidx(*args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [sys.executable, "-m", "code_indexer.cli", *args],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    init = cidx("init", "--vector-store", "filesystem")
    assert init.returncode == 0, init.stdout + init.stderr

    rebuilt = cidx("index", "--rebuild-fts-index", "--content-backend=delegated")

    out = rebuilt.stdout + rebuilt.stderr
    assert rebuilt.returncode == 0, out
    assert f"Files indexed: {len(SHAPES)}" in out, out
    assert not any((project / rel).exists() for rel in SHAPES)
