"""Handing bytes over must produce exactly what reading the file produces.

One indexing pass opens a file up to three times today: the text probe, the blob
hash, and chunking. Collapsing those into one read is only safe if the
hand-the-bytes-over path is indistinguishable from the read-it-again path --
including the sha256 content hash and the git blob hash, because the blob hash
seeds every chunk's deterministic point UUID. A difference there would not raise;
it would RE-IDENTIFY chunks.
"""

from __future__ import annotations

import builtins
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.config import Config, IndexingConfig
from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
from code_indexer.services.file_identifier import FileIdentifier

SHAPES: Dict[str, bytes] = {
    "lf.py": b"def a():\n    return 1\n",
    "crlf.py": b"def a():\r\n    return 1\r\n",
    "cr.py": b"def a():\r    return 1\r",
    "bom.py": "﻿def a():\n    pass\n".encode("utf-8"),
    "latin1.py": b"x = 'caf\xe9'\n",
    "unicode.py": "s = 'café — \U0001f600'\n".encode("utf-8"),
    "empty.py": b"",
    "big.py": b"line\n" * 5000,
    "no_newline.py": b"last = 1",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A REAL git repo, not just a directory.

    FileIdentifier only computes the git blob hash when git_available is true;
    in a plain directory it takes the filesystem-metadata branch and git_hash is
    absent. A mutation that corrupted the blob hash therefore passed unnoticed --
    both sides compared None to None. The blob hash is the one value that seeds
    every chunk's point UUID, so it is exactly the branch that must be exercised.
    """
    root = tmp_path / "repo"
    root.mkdir()
    for name, raw in SHAPES.items():
        (root / name).write_bytes(raw)

    def run(*a: str) -> None:
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    run("add", "-A")
    run("commit", "-q", "-m", "fixture")
    return root


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_chunks_are_identical_whether_bytes_are_handed_over_or_read(
    repo: Path, name: str
) -> None:
    chunker = FixedSizeChunker(IndexingConfig())
    target = repo / name

    from_disk = chunker.chunk_file(target, repo_root=repo)
    handed = chunker.chunk_file(target, repo_root=repo, content=target.read_bytes())

    assert [c.get("text") for c in handed] == [c.get("text") for c in from_disk], name


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_metadata_hashes_are_identical_whether_bytes_are_handed_over_or_read(
    repo: Path, name: str
) -> None:
    identifier = FileIdentifier(repo, Config(codebase_dir=repo))
    target = repo / name

    from_disk = identifier.get_file_metadata(target)
    handed = identifier.get_file_metadata(target, content=target.read_bytes())

    assert from_disk["git_available"], (
        "fixture must be a git repo or the blob-hash branch is dead"
    )
    assert from_disk.get("git_hash"), f"{name}: no blob hash computed"
    assert handed["file_hash"] == from_disk["file_hash"], f"{name}: sha256 differs"
    assert handed.get("git_hash") == from_disk.get("git_hash"), (
        f"{name}: blob hash differs"
    )


def test_handing_bytes_over_removes_the_second_read(repo: Path) -> None:
    """The point of the parameter: the file is not opened again.

    Counted, not timed -- a read either happens or it does not, on any machine.
    """
    target = repo / "lf.py"
    data = target.read_bytes()

    opened: List[str] = []
    real_open = builtins.open

    def spy(file: Any, *a: Any, **k: Any) -> Any:
        if str(file) == str(target):
            opened.append("open")
        return real_open(file, *a, **k)

    real_read_bytes = Path.read_bytes

    def spy_rb(self: Path) -> bytes:
        if str(self) == str(target):
            opened.append("read_bytes")
        return real_read_bytes(self)

    chunker = FixedSizeChunker(IndexingConfig())
    identifier = FileIdentifier(repo, Config(codebase_dir=repo))

    builtins.open = spy  # type: ignore[assignment]
    Path.read_bytes = spy_rb  # type: ignore[method-assign]
    try:
        opened.clear()
        identifier.get_file_metadata(target)
        chunker.chunk_file(target, repo_root=repo)
        reads_before = len(opened)

        opened.clear()
        identifier.get_file_metadata(target, content=data)
        chunker.chunk_file(target, repo_root=repo, content=data)
        reads_after = len(opened)
    finally:
        builtins.open = real_open  # type: ignore[assignment]
        Path.read_bytes = real_read_bytes  # type: ignore[method-assign]

    assert reads_before >= 2, (
        f"expected the un-collapsed path to read twice, saw {reads_before}"
    )
    assert reads_after == 0, (
        f"handing bytes over still read the file {reads_after} time(s)"
    )
