"""The indexing pass reads file bytes through the content backend, a batch at a time.

What is pinned here, and the mutation each test exists to catch:

* **Identity.** The bytes a worker gets through the seam are the bytes on disk,
  for every shape that has bitten encoding before (CRLF, BOM, latin-1, binary,
  empty). On the high-throughput path a chunk's point id is seeded from
  sha256(bytes), so any difference re-identifies chunks instead of failing.
* **Call shape.** One fetch per PLANNED batch, never one per file -- measured
  across the batch boundary, where an off-by-one hides.
* **Bytes.** Only selected files cross; a large unselected binary never does.
* **Staleness.** A file that changed since the manifest is refused, not indexed
  under the manifest's hash.
* **No disk.** With a content source the worker never touches the local path --
  proven against a path that does not exist, not by patching ``open``.
"""

from __future__ import annotations

import errno
import math
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence
from unittest.mock import MagicMock

import pytest

from code_indexer.indexing.content_prefetcher import (
    CHANGED_SINCE_MANIFEST,
    ContentPrefetcher,
)
from code_indexer.server.storage.shared.content_backend import (
    FetchedFile,
    LocalContentBackend,
)
from code_indexer.utils.blob_hash import git_blob_hash_bytes

SHAPES: Dict[str, bytes] = {
    "lf.py": b"def a():\n    return 1\n",
    "crlf.py": b"def a():\r\n    return 1\r\n",
    "bom.py": "﻿def a():\n    pass\n".encode("utf-8"),
    "latin1.py": b"x = 'caf\xe9'\n",
    "unicode.py": "s = 'café \U0001f600'\n".encode("utf-8"),
    "empty.py": b"",
    "nested/deep/mod.py": b"import os\n",
    "binary.bin": bytes(range(256)) * 4,
}


class _Delegated:
    """The local backend presented as a delegated one, with every call recorded."""

    is_local = False

    def __init__(self) -> None:
        self._inner = LocalContentBackend()
        self.calls: List[List[str]] = []
        self.bytes_served = 0
        self._lock = threading.Lock()

    def manifest(self, *a: Any, **k: Any) -> Any:
        return self._inner.manifest(*a, **k)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        with self._lock:
            self.calls.append(list(paths))
        for record in self._inner.fetch(repo_path, paths):
            with self._lock:
                self.bytes_served += len(record.data)
            yield record


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel, data in SHAPES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def _prefetcher(
    backend: Any,
    root: Path,
    paths: Sequence[str],
    *,
    sizes: Optional[Dict[str, int]] = None,
    shas: Optional[Dict[str, str]] = None,
    budget_bytes: int = 8 * 1024 * 1024,
    budget_files: int = 1000,
) -> ContentPrefetcher:
    def size_of(rel: str) -> Optional[int]:
        if sizes is not None:
            return sizes.get(rel)
        return (root / rel).stat().st_size

    return ContentPrefetcher(
        backend,
        root,
        paths,
        size_of=size_of,
        expected_blob_sha=(shas or {}).get,
        budget_bytes=budget_bytes,
        budget_files=budget_files,
    )


# ── identity ───────────────────────────────────────────────────────────────


def test_every_shape_arrives_byte_identical_to_disk(repo: Path) -> None:
    backend = _Delegated()
    pf = _prefetcher(backend, repo, list(SHAPES))
    for rel, raw in SHAPES.items():
        got = pf.take(repo / rel)
        assert got.error is None, (rel, got.error)
        assert got.data == raw, f"{rel}: fetched bytes differ from disk"


def test_matching_manifest_hashes_are_accepted(repo: Path) -> None:
    """The positive half of the staleness check: unchanged files go through."""
    shas = {rel: git_blob_hash_bytes(raw) for rel, raw in SHAPES.items()}
    pf = _prefetcher(_Delegated(), repo, list(SHAPES), shas=shas)
    for rel in SHAPES:
        assert pf.take(repo / rel).data == SHAPES[rel]


# ── call shape ─────────────────────────────────────────────────────────────

_BUDGET_FILES = 4


@pytest.mark.parametrize(
    "n", [_BUDGET_FILES - 1, _BUDGET_FILES, _BUDGET_FILES + 1, 10 * _BUDGET_FILES]
)
def test_one_fetch_per_planned_batch_never_per_file(tmp_path: Path, n: int) -> None:
    root = tmp_path / "r"
    root.mkdir()
    paths = [f"f{i:03}.py" for i in range(n)]
    for rel in paths:
        (root / rel).write_bytes(b"x = 1\n")
    backend = _Delegated()
    pf = _prefetcher(backend, root, paths, budget_files=_BUDGET_FILES)

    for rel in paths:
        assert pf.take(root / rel).data == b"x = 1\n"

    assert len(backend.calls) == math.ceil(n / _BUDGET_FILES), backend.calls
    assert sorted(p for call in backend.calls for p in call) == paths


def test_the_byte_budget_bounds_each_request(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    sizes = {"a.py": 60, "b.py": 60, "c.py": 60}
    for rel, size in sizes.items():
        (root / rel).write_bytes(b"x" * size)
    backend = _Delegated()
    pf = _prefetcher(backend, root, list(sizes), sizes=sizes, budget_bytes=100)
    for rel in sizes:
        pf.take(root / rel)
    assert backend.calls == [["a.py"], ["b.py"], ["c.py"]]


def test_unselected_files_never_cross(repo: Path) -> None:
    """Selection is the caller's: a binary nobody asked for moves zero bytes."""
    backend = _Delegated()
    selected = [rel for rel in SHAPES if not rel.endswith(".bin")]
    pf = _prefetcher(backend, repo, selected)
    for rel in selected:
        pf.take(repo / rel)
    assert backend.bytes_served == sum(len(SHAPES[rel]) for rel in selected)
    assert all("binary.bin" not in call for call in backend.calls)


def test_concurrent_workers_on_one_batch_fetch_it_once(tmp_path: Path) -> None:
    """Workers drain the pool together; the first one in fetches for the rest."""
    root = tmp_path / "r"
    root.mkdir()
    paths = [f"f{i}.py" for i in range(16)]
    for rel in paths:
        (root / rel).write_bytes(rel.encode())
    backend = _Delegated()
    pf = _prefetcher(backend, root, paths)
    start = threading.Barrier(len(paths))
    results: Dict[str, Optional[bytes]] = {}

    def worker(rel: str) -> None:
        start.wait()
        results[rel] = pf.take(root / rel).data

    threads = [threading.Thread(target=worker, args=(rel,)) for rel in paths]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(backend.calls) == 1
    assert results == {rel: rel.encode() for rel in paths}


def test_a_batch_whose_files_all_vanished_before_take_is_never_fetched(
    repo: Path,
) -> None:
    """Files dropped in the hash phase are never taken; their batch costs nothing."""
    backend = _Delegated()
    _prefetcher(backend, repo, list(SHAPES))
    assert backend.calls == []


# ── refusals ───────────────────────────────────────────────────────────────


def test_a_file_changed_since_the_manifest_is_refused(repo: Path) -> None:
    """Its metadata already carries the manifest's hash; these bytes do not match."""
    pf = _prefetcher(_Delegated(), repo, ["lf.py"], shas={"lf.py": "0" * 40})
    got = pf.take(repo / "lf.py")
    assert got.data is None
    assert got.error == CHANGED_SINCE_MANIFEST
    assert not got.vanished


@pytest.mark.parametrize("reported", ["ENOENT", os.strerror(errno.ENOENT)])
def test_gone_is_vanished_in_either_spelling(repo: Path, reported: str) -> None:
    """workspace-mcp says ENOENT; the local backend says strerror. Both mean gone."""

    class _Gone(_Delegated):
        def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[Any]:
            for p in paths:
                yield FetchedFile(path=p, blob_sha=None, data=b"", error=reported)

    got = _prefetcher(_Gone(), repo, ["lf.py"]).take(repo / "lf.py")
    assert got.vanished


def test_an_unreadable_file_is_a_failure_not_a_vanish(repo: Path) -> None:
    class _Denied(_Delegated):
        def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[Any]:
            for p in paths:
                yield FetchedFile(path=p, blob_sha=None, data=b"", error="EACCES")

    got = _prefetcher(_Denied(), repo, ["lf.py"]).take(repo / "lf.py")
    assert got.error == "EACCES"
    assert not got.vanished


def test_a_transport_failure_fails_its_batch_only(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    paths = ["a.py", "b.py", "c.py", "d.py"]
    for rel in paths:
        (root / rel).write_bytes(b"1")

    class _Flaky(_Delegated):
        def fetch(self, repo_path: Any, ps: Sequence[str]) -> Iterator[Any]:
            if "a.py" in ps:
                raise ConnectionError("reset by peer")
            return super().fetch(repo_path, ps)

    pf = _prefetcher(_Flaky(), root, paths, budget_files=2)
    assert [pf.take(root / p).error is not None for p in paths] == [
        True,
        True,
        False,
        False,
    ]


def test_a_path_is_served_once(repo: Path) -> None:
    """Bytes are dropped on take; holding them for a second caller is the leak."""
    pf = _prefetcher(_Delegated(), repo, ["lf.py"])
    assert pf.take(repo / "lf.py").data is not None
    again = pf.take(repo / "lf.py")
    assert again.data is None
    # Refused as already taken -- not misreported as a backend that sent nothing.
    assert again.error == "not planned for this pass"


def test_bytes_are_released_on_take_not_when_the_batch_finishes(
    tmp_path: Path,
) -> None:
    """A batch spans many workers; holding taken bytes until the last one
    finishes multiplies peak memory by the batch's width."""
    root = tmp_path / "r"
    root.mkdir()
    for rel in ("a.py", "b.py"):
        (root / rel).write_bytes(b"x" * 100)
    pf = _prefetcher(_Delegated(), root, ["a.py", "b.py"])
    pending = pf._batch_of["b.py"]

    pf.take(root / "a.py")

    assert pending.files is not None
    assert "a.py" not in pending.files, "a.py's bytes outlived its take"


def test_a_path_outside_the_root_is_refused_without_a_fetch(repo: Path) -> None:
    backend = _Delegated()
    got = _prefetcher(backend, repo, ["lf.py"]).take(repo.parent / "elsewhere.py")
    assert got.data is None and got.error
    assert backend.calls == []


# ── the worker: no disk when a content source is set ───────────────────────


def _manager(codebase_dir: Path, content_source: Any) -> Any:
    from code_indexer.services.clean_slot_tracker import CleanSlotTracker
    from code_indexer.services.file_chunking_manager import FileChunkingManager

    return FileChunkingManager(
        vector_manager=MagicMock(),
        chunker=MagicMock(),
        vector_store_client=MagicMock(),
        thread_count=1,
        slot_tracker=CleanSlotTracker(max_slots=3),
        codebase_dir=codebase_dir,
        content_source=content_source,
    )


def test_the_worker_reads_through_the_source_with_no_repo_on_disk(
    tmp_path: Path,
) -> None:
    """The repo path does not exist here, so any local read would fail by itself."""
    absent = tmp_path / "not-mounted"
    source = MagicMock()
    source.take.return_value = MagicMock(vanished=False, error=None, data=b"x = 1\n")
    assert _manager(absent, source)._read_content(absent / "a.py") == b"x = 1\n"


def test_the_worker_treats_a_vanished_file_as_vanished(tmp_path: Path) -> None:
    source = MagicMock()
    source.take.return_value = MagicMock(vanished=True, error="ENOENT", data=None)
    with pytest.raises(FileNotFoundError):
        _manager(tmp_path, source)._read_content(tmp_path / "a.py")


def test_the_worker_never_falls_back_to_disk_on_a_refusal(tmp_path: Path) -> None:
    """The file IS on disk here; a fallback would quietly return it."""
    (tmp_path / "a.py").write_bytes(b"on disk")
    source = MagicMock()
    source.take.return_value = MagicMock(
        vanished=False, error=CHANGED_SINCE_MANIFEST, data=None
    )
    from code_indexer.services.file_chunking_manager import ContentUnavailableError

    with pytest.raises(ContentUnavailableError):
        _manager(tmp_path, source)._read_content(tmp_path / "a.py")


def test_without_a_source_the_worker_reads_the_disk(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"on disk")
    assert _manager(tmp_path, None)._read_content(tmp_path / "a.py") == b"on disk"


# ── the pass: a source only when a delegated manifest exists ───────────────


def _pass(tmp_path: Path, manifest: Any) -> Any:
    from code_indexer.config import Config
    from code_indexer.services.high_throughput_processor import (
        HighThroughputProcessor,
    )

    proc = HighThroughputProcessor.__new__(HighThroughputProcessor)
    proc.config = Config(codebase_dir=tmp_path)
    proc.content_backend = _Delegated()
    proc.repo_manifest = manifest
    return proc


def test_no_manifest_means_no_source(tmp_path: Path) -> None:
    """Local passes build no manifest, so they keep reading the disk unchanged."""
    assert _pass(tmp_path, None)._build_content_source([tmp_path / "a.py"]) is None


def test_a_delegated_pass_serves_its_files_through_the_backend(repo: Path) -> None:
    from code_indexer.indexing.repo_manifest import RepoManifest

    proc = _pass(repo, None)
    proc.repo_manifest = RepoManifest.build(repo, backend=proc.content_backend)
    files = [repo / rel for rel in SHAPES]

    source = proc._build_content_source(files)

    assert source is not None
    for rel, raw in SHAPES.items():
        assert source.take(repo / rel).data == raw
    assert len(proc.content_backend.calls) == 1, "one batch for a small repo"
