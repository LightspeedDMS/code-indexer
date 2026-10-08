"""Under a delegated backend, a file's size and mtime come from the manifest.

The repository is not on this container's disk, so a ``stat`` either fails or --
while a mount is still present -- answers about a copy the index is not being
built from. What is pinned, and the mutation each test catches:

* ``RepoManifest.mtime`` is the SAME float ``os.stat().st_mtime`` returns, bit for
  bit. Mtimes are stored in point payloads and compared later; a value off in
  the last bit reads as "modified" forever. ``ns / 1e9`` is not that float.
* The local backend reports ``st_mtime_ns`` -- a symlink's own, never its
  target's -- so local and delegated manifests carry the same field.
* With a manifest there is no stat at all: proven against a path that does not
  exist, so any local stat would raise by itself.
* Without one, behaviour is exactly the old ``stat``, including its exceptions.
"""

from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from code_indexer.indexing import file_facts
from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import (
    ContentEntry,
    LocalContentBackend,
)


def _manifest(root: Path, **entries: ContentEntry) -> RepoManifest:
    return RepoManifest(root, dict(entries), backend_is_local=False)


def _entry(path: str, *, size: int = 7, mtime_ns: Optional[int] = None) -> Any:
    return ContentEntry(
        path=path, size=size, blob_sha="a" * 40, is_symlink=False, mtime_ns=mtime_ns
    )


# ── the float is exactly st_mtime ──────────────────────────────────────────


def test_mtime_is_bit_identical_to_a_local_stat(tmp_path: Path) -> None:
    """Many sub-second times, so a rounding formula would differ on some of them."""
    rng = random.Random(1234)
    paths = []
    for i in range(300):
        p = tmp_path / f"f{i}.py"
        p.write_bytes(b"x")
        ns = 1_600_000_000 * 10**9 + rng.randrange(10**9) * rng.randrange(1, 300)
        os.utime(p, ns=(ns, ns))
        paths.append(p)

    manifest = RepoManifest.build(tmp_path, backend=LocalContentBackend())

    for p in paths:
        assert manifest.mtime(p) == p.stat().st_mtime, p.name


def test_the_naive_division_is_not_that_float() -> None:
    """Guards the test above against a fixture too tame to tell the two apart."""
    rng = random.Random(1234)
    samples = [
        1_600_000_000 * 10**9 + rng.randrange(10**9) * rng.randrange(1, 300)
        for _ in range(300)
    ]
    assert any(
        ns / 1e9 != divmod(ns, 10**9)[0] + divmod(ns, 10**9)[1] * 1e-9 for ns in samples
    )


@pytest.mark.parametrize("given", ["missing", None, "attribute-absent"])
def test_an_unknown_mtime_is_None_never_the_epoch(tmp_path: Path, given: str) -> None:
    """Absent entry, a null field, or a plugin entry type predating the field."""

    class _OldPluginEntry:
        path = "a.py"
        size = 1
        blob_sha = "a" * 40
        is_symlink = False

    entries: Dict[str, Any] = {}
    if given is None:
        entries["a.py"] = _entry("a.py", mtime_ns=None)
    elif given == "attribute-absent":
        entries["a.py"] = _OldPluginEntry()
    manifest = RepoManifest(tmp_path, entries, backend_is_local=False)
    assert manifest.mtime(tmp_path / "a.py") is None


# ── the local backend reports it ───────────────────────────────────────────


def test_the_local_backend_reports_st_mtime_ns(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_bytes(b"x")
    os.utime(f, ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
    (entry,) = LocalContentBackend().manifest(tmp_path).entries
    assert entry.mtime_ns == f.stat().st_mtime_ns == 1_700_000_000_123_456_789


def test_a_symlinks_mtime_is_its_own_not_its_targets(tmp_path: Path) -> None:
    target = tmp_path / "real.py"
    target.write_bytes(b"x")
    link = tmp_path / "link.py"
    link.symlink_to(target)
    os.utime(target, ns=(1_600_000_000 * 10**9, 1_600_000_000 * 10**9))
    os.utime(
        link, ns=(1_700_000_000 * 10**9, 1_700_000_000 * 10**9), follow_symlinks=False
    )

    entries = {e.path: e for e in LocalContentBackend().manifest(tmp_path).entries}

    assert entries["link.py"].mtime_ns == link.lstat().st_mtime_ns
    assert entries["link.py"].mtime_ns != target.stat().st_mtime_ns


# ── file_facts: manifest when present, never a stat ────────────────────────


def test_with_a_manifest_nothing_is_statted(tmp_path: Path) -> None:
    """The path does not exist, so a stat anywhere would raise on its own."""
    absent = tmp_path / "not-mounted"
    m = _manifest(absent, **{"a.py": _entry("a.py", size=42, mtime_ns=5 * 10**9)})
    assert file_facts.file_size(absent / "a.py", m) == 42
    assert file_facts.file_mtime_and_size(absent / "a.py", m) == (5.0, 42)


def test_a_manifest_miss_is_unknown_not_a_stat(tmp_path: Path) -> None:
    """The file IS on disk here; a fallback would quietly report its real size."""
    (tmp_path / "late.py").write_bytes(b"x" * 99)
    m = _manifest(tmp_path)
    assert file_facts.file_size(tmp_path / "late.py", m) == 0
    assert file_facts.file_mtime_and_size(tmp_path / "late.py", m) == (None, 0)


def test_without_a_manifest_it_is_exactly_a_stat(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_bytes(b"x" * 13)
    assert file_facts.file_size(f, None) == 13
    assert file_facts.file_mtime_and_size(f, None) == (f.stat().st_mtime, 13)


def test_without_a_manifest_a_missing_file_still_raises(tmp_path: Path) -> None:
    """Callers' vanished-file handling depends on this FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        file_facts.file_size(tmp_path / "gone.py", None)
    with pytest.raises(FileNotFoundError):
        file_facts.file_mtime_and_size(tmp_path / "gone.py", None)


# ── the stored payload timestamp ───────────────────────────────────────────


def test_the_payload_records_the_manifests_mtime(tmp_path: Path) -> None:
    """file_last_modified is what later passes compare; it must be the manifest's."""
    from unittest.mock import MagicMock

    from code_indexer.services.clean_slot_tracker import CleanSlotTracker
    from code_indexer.services.file_chunking_manager import FileChunkingManager

    absent = tmp_path / "not-mounted"
    ns = 1_700_000_000_123_456_789
    m = _manifest(absent, **{"a.py": _entry("a.py", size=11, mtime_ns=ns)})
    manager = FileChunkingManager(
        vector_manager=MagicMock(),
        chunker=MagicMock(),
        vector_store_client=MagicMock(),
        thread_count=1,
        slot_tracker=CleanSlotTracker(max_slots=3),
        codebase_dir=absent,
        manifest=m,
    )
    chunk = {
        "text": "x = 1\n",
        "chunk_index": 0,
        "total_chunks": 1,
        "file_extension": "py",
        "line_start": 1,
        "line_end": 1,
    }
    metadata = {
        "project_id": "p",
        "file_hash": "h",
        "git_available": False,
        "collection_name": "c",
        "file_mtime": 0.0,
        "file_size": 0,
    }

    point = manager._create_vector_point(chunk, [0.0], metadata, absent / "a.py")

    expected = divmod(ns, 10**9)[0] + divmod(ns, 10**9)[1] * 1e-9
    assert point["payload"]["file_last_modified"] == expected
    assert point["payload"]["file_size"] == 11


def test_the_pass_hands_its_manifest_to_the_chunking_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the hand-off every site above silently keeps statting."""
    from unittest.mock import MagicMock

    from code_indexer.config import Config
    from code_indexer.services import high_throughput_processor as htp

    class _Stop(Exception):
        pass

    seen: Dict[str, Any] = {}

    def recording_manager(**kwargs: Any) -> Any:
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(htp, "FileChunkingManager", recording_manager)
    monkeypatch.setattr(htp, "VectorCalculationManager", MagicMock())
    proc = htp.HighThroughputProcessor.__new__(htp.HighThroughputProcessor)
    proc.config = Config(codebase_dir=tmp_path)
    proc.embedding_provider = MagicMock()
    proc.vector_store_client = MagicMock()
    proc.fixed_size_chunker = MagicMock()
    proc.repo_manifest = _manifest(tmp_path)
    # A manifest also means a content source is built from the backend.
    proc.content_backend = MagicMock()
    # Rate tracking is __init__ state this bare instance never got; irrelevant here.
    monkeypatch.setattr(proc, "_initialize_file_rate_tracking", lambda: None)

    with pytest.raises(_Stop):
        proc._process_files_high_throughput([], vector_thread_count=1)

    assert seen["manifest"] is proc.repo_manifest


# ── the size-only sites: a stat there is not cosmetic ──────────────────────


def test_the_slot_reports_the_manifests_size_not_the_disks(tmp_path: Path) -> None:
    """On disk 5 bytes, in the manifest 42: a stat would report the wrong copy."""
    from unittest.mock import MagicMock

    from code_indexer.services.file_chunking_manager import FileChunkingManager

    (tmp_path / "a.py").write_bytes(b"x = 1")
    m = _manifest(tmp_path, **{"a.py": _entry("a.py", size=42, mtime_ns=10**9)})
    chunker = MagicMock()
    chunker.chunk_file.return_value = []  # empty file: returns before embedding
    tracker = MagicMock()
    manager = FileChunkingManager(
        vector_manager=MagicMock(),
        chunker=chunker,
        vector_store_client=MagicMock(),
        thread_count=1,
        slot_tracker=tracker,
        codebase_dir=tmp_path,
        manifest=m,
    )

    manager._process_file_clean_lifecycle(
        tmp_path / "a.py", {"file_hash": "h"}, None, tracker
    )

    (file_data,) = tracker.acquire_slot.call_args.args
    assert file_data.file_size == 42


def test_the_hash_phase_keeps_a_file_it_cannot_stat_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no mount, a stat raises FileNotFoundError and the hash phase drops the
    file as vanished -- every file, on a delegated pod. It must reach submission."""
    from unittest.mock import MagicMock

    from code_indexer.config import Config
    from code_indexer.services import high_throughput_processor as htp

    class _Submitted(BaseException):
        """BaseException: the submission loop swallows Exception per file."""

    class _RecordingManager:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "_RecordingManager":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def submit_file_for_processing(self, file_path: Path, *a: Any) -> Any:
            raise _Submitted(str(file_path))

    repo = tmp_path / "repo"  # a.py is in the manifest, NOT on disk
    monkeypatch.setattr(htp, "FileChunkingManager", _RecordingManager)
    monkeypatch.setattr(htp, "VectorCalculationManager", MagicMock())
    proc = htp.HighThroughputProcessor(
        Config(codebase_dir=repo), MagicMock(), MagicMock()
    )
    proc.file_identifier = MagicMock()
    proc.file_identifier.get_file_metadata.return_value = {"file_hash": None}
    proc.repo_manifest = _manifest(
        repo, **{"a.py": _entry("a.py", size=3, mtime_ns=10**9)}
    )
    assert not (repo / "a.py").exists()

    with pytest.raises(_Submitted, match="a.py"):
        proc._process_files_high_throughput([repo / "a.py"], vector_thread_count=1)
