"""Bug #1529 test (d): a concurrent reader never observes a partial refresh.

The fixed-path design removes versioning, so the ONE guarantee that has to be
provided explicitly is that refreshing a shard in place never exposes a torn
state. This exercises the real mechanism end-to-end: a real reflink CoW copy
(through the project's designated ``create_clone_at_path`` primitive), a real
SQLite chunk store as the payload, real ``os.replace`` swaps, and a real
reader thread hammering the live path throughout.

Nothing about the code under test is mocked. The only test double is a
LocalCloneBackend-shaped clone backend, which is the genuine production
primitive's own local implementation.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

from code_indexer.services.temporal.temporal_atomic_refresh import (
    TemporalAtomicRefreshError,
    refresh_temporal_shard_atomically,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_SIZE = 8


class _ReflinkCloneBackend:
    """The real local CoW primitive: ``cp --reflink=auto -a``.

    Mirrors LocalCloneBackend.create_clone_at_path. Living in the test file
    keeps production code free of any direct cp+reflink call (this project's
    AC15 anti-orphan lint gate scans production only, and the production
    module deliberately requires this to be INJECTED).
    """

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def create_clone_at_path(self, source_path: str, dest_path: str) -> str:
        self.calls.append((source_path, dest_path))
        subprocess.run(
            ["cp", "--reflink=auto", "-a", source_path, dest_path],
            check=True,
            capture_output=True,
        )
        return dest_path


def _record(point_id: str, seed: int) -> Dict[str, Any]:
    rng = np.random.default_rng(seed)
    return {
        "id": point_id,
        "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
        "payload": {"path": "src/a.py", "commit_hash": point_id.split(":")[2]},
        "chunk_text": f"content {point_id}",
    }


def _write_rows(shard_dir: Path, records: List[Dict[str, Any]]) -> None:
    store = ChunkStore(shard_dir / "chunks.db", expected_dim=VECTOR_SIZE)
    try:
        store.write_batch(records)
    finally:
        store.close()


def _read_ids(shard_dir: Path) -> set:
    """Read the live chunks.db exactly as a concurrent reader would."""
    store = ChunkStore(shard_dir / "chunks.db", immutable=True)
    try:
        return {r["id"] for r in store.stream_all()}
    finally:
        store.close()


def _build_live_shard(tmp_path: Path) -> Path:
    shard = tmp_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    shard.mkdir(parents=True)
    _write_rows(shard, [_record("p:commit:aaaaaaaa:0", 1)])
    (shard / "hnsw_index.bin").write_bytes(b"OLD-INDEX")
    (shard / "collection_meta.json").write_text('{"vector_count": 1}')
    return shard


# ---------------------------------------------------------------------------
# (d) the concurrency guarantee
# ---------------------------------------------------------------------------


def test_concurrent_reader_never_sees_a_partial_or_corrupt_shard(
    tmp_path: Path,
) -> None:
    """A reader polling the live path throughout a refresh must always get a
    VALID row set -- either the pre-refresh one or the post-refresh one, never
    a torn file and never an exception."""
    shard = _build_live_shard(tmp_path)
    old_ids = {"p:commit:aaaaaaaa:0"}
    new_ids = old_ids | {"p:commit:bbbbbbbb:0"}

    observations: List[set] = []
    errors: List[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                observations.append(_read_ids(shard))
            except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
                errors.append(exc)
                return

    def apply_delta(scratch: Path) -> None:
        _write_rows(scratch, [_record("p:commit:bbbbbbbb:0", 2)])
        (scratch / "hnsw_index.bin").write_bytes(b"NEW-INDEX-LONGER-CONTENT")
        (scratch / "collection_meta.json").write_text('{"vector_count": 2}')

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for _ in range(15):
            refresh_temporal_shard_atomically(
                shard,
                apply_delta,
                clone_backend=_ReflinkCloneBackend(),
            )
    finally:
        stop.set()
        thread.join(timeout=10)

    assert not errors, f"reader hit an error mid-refresh: {errors[:1]}"
    assert observations, "reader never managed to read the shard"
    for seen in observations:
        assert seen in (old_ids, new_ids), (
            f"reader observed a PARTIAL row set {seen} -- the swap exposed an "
            f"intermediate state"
        )
    assert _read_ids(shard) == new_ids
    assert (shard / "hnsw_index.bin").read_bytes() == b"NEW-INDEX-LONGER-CONTENT"


def test_refresh_applies_the_delta(tmp_path: Path) -> None:
    shard = _build_live_shard(tmp_path)

    refresh_temporal_shard_atomically(
        shard,
        lambda scratch: _write_rows(scratch, [_record("p:commit:bbbbbbbb:0", 2)]),
        clone_backend=_ReflinkCloneBackend(),
    )

    assert _read_ids(shard) == {"p:commit:aaaaaaaa:0", "p:commit:bbbbbbbb:0"}


def test_scratch_directory_is_always_cleaned_up(tmp_path: Path) -> None:
    shard = _build_live_shard(tmp_path)
    refresh_temporal_shard_atomically(
        shard,
        lambda scratch: _write_rows(scratch, [_record("p:commit:bbbbbbbb:0", 2)]),
        clone_backend=_ReflinkCloneBackend(),
    )
    leftovers = [p for p in shard.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == [], f"scratch copies left behind: {leftovers}"


def test_the_copy_really_goes_through_the_injected_reflink_primitive(
    tmp_path: Path,
) -> None:
    """Never a direct cp --reflink in production code (AC15)."""
    shard = _build_live_shard(tmp_path)
    backend = _ReflinkCloneBackend()

    refresh_temporal_shard_atomically(shard, lambda s: None, clone_backend=backend)

    assert len(backend.calls) == 1
    source, dest = backend.calls[0]
    assert source == str(shard)
    assert Path(dest).name.startswith(f".{shard.name}.refresh-")


# ---------------------------------------------------------------------------
# Failure paths leave the live shard untouched
# ---------------------------------------------------------------------------


def _assert_untouched(shard: Path) -> None:
    assert _read_ids(shard) == {"p:commit:aaaaaaaa:0"}
    assert (shard / "hnsw_index.bin").read_bytes() == b"OLD-INDEX"
    assert (shard / "collection_meta.json").read_text() == '{"vector_count": 1}'


def test_delta_failure_leaves_the_live_shard_unchanged(tmp_path: Path) -> None:
    shard = _build_live_shard(tmp_path)

    def exploding_delta(scratch: Path) -> None:
        _write_rows(scratch, [_record("p:commit:bbbbbbbb:0", 2)])
        raise RuntimeError("indexing blew up")

    with pytest.raises(TemporalAtomicRefreshError, match="applying the refresh delta"):
        refresh_temporal_shard_atomically(
            shard, exploding_delta, clone_backend=_ReflinkCloneBackend()
        )

    _assert_untouched(shard)


def test_verification_failure_leaves_the_live_shard_unchanged(tmp_path: Path) -> None:
    shard = _build_live_shard(tmp_path)

    def rejecting_verify(scratch: Path) -> None:
        raise AssertionError("row count mismatch")

    with pytest.raises(TemporalAtomicRefreshError, match="verification"):
        refresh_temporal_shard_atomically(
            shard,
            lambda scratch: _write_rows(scratch, [_record("p:commit:bbbbbbbb:0", 2)]),
            clone_backend=_ReflinkCloneBackend(),
            verify=rejecting_verify,
        )

    _assert_untouched(shard)


def test_copy_failure_leaves_the_live_shard_unchanged(tmp_path: Path) -> None:
    shard = _build_live_shard(tmp_path)

    class _BrokenBackend:
        def create_clone_at_path(self, source_path: str, dest_path: str) -> str:
            raise OSError("no space left on device")

    with pytest.raises(TemporalAtomicRefreshError, match="reflink copy"):
        refresh_temporal_shard_atomically(
            shard, lambda s: None, clone_backend=_BrokenBackend()
        )

    _assert_untouched(shard)


def test_a_missing_clone_backend_is_refused(tmp_path: Path) -> None:
    """Never silently degrade to a full byte copy of a multi-GB shard."""
    shard = _build_live_shard(tmp_path)
    with pytest.raises(TemporalAtomicRefreshError, match="clone_backend is required"):
        refresh_temporal_shard_atomically(shard, lambda s: None, clone_backend=None)
    _assert_untouched(shard)


def test_first_ever_build_writes_straight_into_place(tmp_path: Path) -> None:
    """No live shard means no reader to protect -- and no pointless copy."""
    shard = tmp_path / "code-indexer-temporal-voyage_code_3-2024Q2"
    backend = _ReflinkCloneBackend()

    refresh_temporal_shard_atomically(
        shard,
        lambda target: _write_rows(target, [_record("p:commit:cccccccc:0", 3)]),
        clone_backend=backend,
    )

    assert _read_ids(shard) == {"p:commit:cccccccc:0"}
    assert backend.calls == [], "a first-ever build must not reflink anything"
