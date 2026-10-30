"""A snapshot clone never copies into a destination that already exists:
two nodes that pick the same v_<ts> name must not merge into one tree
(real filesystem, real `cp`)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from code_indexer.server.storage.shared.clone_backend import (
    CloneDestinationExistsError,
    LocalCloneBackend,
)

SOURCE_MODE = 0o750


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    src.mkdir()
    (src / "file.txt").write_text("indexed\n")
    (src / ".code-indexer").mkdir()
    (src / ".code-indexer" / "config.json").write_text("{}")
    src.chmod(SOURCE_MODE)
    return src


def test_clone_into_an_existing_destination_fails_and_copies_nothing(
    tmp_path: Path, source: Path
):
    backend = LocalCloneBackend(versioned_base=str(tmp_path / "base"))
    taken = tmp_path / "base" / ".versioned" / "example-repo" / "v_1"
    taken.mkdir(parents=True)
    (taken / "published.txt").write_text("the other node's snapshot\n")

    with pytest.raises(CloneDestinationExistsError):
        backend.create_clone(str(source), "example-repo", "v_1")

    assert sorted(p.name for p in taken.iterdir()) == ["published.txt"]
    assert (taken / "published.txt").read_text() == "the other node's snapshot\n"


def test_clone_into_an_absent_destination_copies_the_source_contents(
    tmp_path: Path, source: Path
):
    backend = LocalCloneBackend(versioned_base=str(tmp_path / "base"))

    created = Path(backend.create_clone(str(source), "example-repo", "v_2"))

    assert (created / "file.txt").read_text() == "indexed\n"
    assert (created / ".code-indexer" / "config.json").read_text() == "{}"
    assert not (created / "source").exists(), "the source must not be nested"
    assert stat.S_IMODE(created.stat().st_mode) == SOURCE_MODE
