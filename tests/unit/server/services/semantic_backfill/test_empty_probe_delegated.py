"""The semantic backfill's "all files empty" probe on a mount-less pod.

``_clone_has_only_empty_files`` separates a placeholder repo (files present,
all blank -> 0 vectors is right) from a vanished clone (fail loud). Under a
delegated content backend this pod's clone directory holds index state only,
so the files are the backend's manifest (sizes) and fetch (bytes): a walk here
would see no files and fail every placeholder repo forever. A listing failure
raises -- the job fails with the real cause, never "no files".
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Union

import pytest

from code_indexer.server.services.semantic_backfill.policy import (
    _EMPTY_PROBE_BYTES,
    _clone_has_only_empty_files,
)
from tests.unit.server.cidx_meta_seam_fakes import MetaWorld


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetaWorld:
    return MetaWorld(tmp_path, monkeypatch)


CASES = [
    ({}, False),  # no files: vanished clone -> fail loud
    ({"README.md": b""}, True),
    ({"README.md": b"  \n\t"}, True),
    ({"README.md": b"", "src/a.py": b"print(1)\n"}, False),
    ({"big.bin": b" " * (_EMPTY_PROBE_BYTES + 1)}, False),
    ({".git/HEAD": b"ref", "a.txt": b""}, True),  # .git never counts
    ({"sub/.code-indexer/x": b"data", "a.txt": b""}, True),
]


def _seed(root: Path, files: Dict[str, Union[bytes, str]]) -> None:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode())


@pytest.mark.parametrize("files, expected", CASES)
def test_local_probe_is_unchanged(
    world: MetaWorld, files: dict, expected: bool
) -> None:
    _seed(world.local_meta, files)
    assert _clone_has_only_empty_files(world.local_meta) is expected


@pytest.mark.parametrize("files, expected", CASES)
def test_delegated_probe_answers_as_the_local_walk(
    world: MetaWorld, files: dict, expected: bool
) -> None:
    _seed(world.remote_meta, files)
    world.delegated()
    with world.forbid_local_io():
        assert _clone_has_only_empty_files(world.pod_meta) is expected


def test_delegated_large_file_is_never_fetched(world: MetaWorld) -> None:
    _seed(world.remote_meta, {"a.txt": b"", "big": b"x" * (_EMPTY_PROBE_BYTES + 1)})
    world.delegated()
    with world.forbid_local_io():
        assert _clone_has_only_empty_files(world.pod_meta) is False
    assert world.backend.fetch_calls == []


def test_delegated_symlink_cannot_confirm_empty(world: MetaWorld) -> None:
    _seed(world.remote_meta, {"a.txt": b""})
    (world.remote_meta / "link").symlink_to(world.remote_meta / "a.txt")
    world.delegated()
    with world.forbid_local_io():
        assert _clone_has_only_empty_files(world.pod_meta) is False


def test_delegated_listing_failure_raises(world: MetaWorld) -> None:
    _seed(world.remote_meta, {"a.txt": b""})
    world.delegated()
    world.backend.fail = RuntimeError("workspace-mcp down")
    with world.forbid_local_io(), pytest.raises(RuntimeError, match="workspace-mcp"):
        _clone_has_only_empty_files(world.pod_meta)
