"""#2093 (confirm review P2-4): watch_metadata.json carries the watch's owed
work, so its writes are serialized and atomic, and an unreadable file is
reported loudly and kept, never silently replaced by a fresh session."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import List

import pytest

from code_indexer.services.watch_metadata import WatchMetadata

_WRITERS = 4
_SAVES_PER_WRITER = 40


def _large(tag: int) -> WatchMetadata:
    metadata = WatchMetadata()
    metadata.files_being_processed = [f"/repo/src/{tag}_{i}.py" for i in range(3000)]
    return metadata


def test_concurrent_saves_never_expose_partial_json(tmp_path: Path) -> None:
    path = tmp_path / ".code-indexer" / "watch_metadata.json"
    _large(0).save_to_disk(path)
    done = threading.Event()
    unparseable: List[str] = []

    def read_until_done() -> None:
        while not done.is_set():
            try:
                json.loads(path.read_text())
            except ValueError as e:
                unparseable.append(str(e))

    def write(tag: int) -> None:
        metadata = _large(tag)
        for _ in range(_SAVES_PER_WRITER):
            metadata.save_to_disk(path)

    reader = threading.Thread(target=read_until_done)
    writers = [threading.Thread(target=write, args=(t,)) for t in range(_WRITERS)]
    reader.start()
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(timeout=60)
    done.set()
    reader.join(timeout=60)

    assert unparseable == []


def test_a_save_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / ".code-indexer" / "watch_metadata.json"

    _large(1).save_to_disk(path)

    assert sorted(p.name for p in path.parent.iterdir()) == ["watch_metadata.json"]


def test_an_unreadable_record_is_loud_and_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / ".code-indexer" / "watch_metadata.json"
    path.parent.mkdir()
    path.write_text('{"files_being_processed": ["/repo/src/a.py"')

    with caplog.at_level(logging.ERROR):
        metadata = WatchMetadata.load_from_disk(path)

    assert metadata.processing_interrupted is True
    assert "unreadable" in (metadata.last_error or "")
    assert any(
        r.levelno == logging.ERROR and str(path) in r.getMessage()
        for r in caplog.records
    )
    kept = [p for p in path.parent.iterdir() if p.name != "watch_metadata.json"]
    assert [p.read_text() for p in kept] == [
        '{"files_being_processed": ["/repo/src/a.py"'
    ]


def test_an_earlier_unreadable_record_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / ".code-indexer" / "watch_metadata.json"
    path.parent.mkdir()
    (path.parent / "watch_metadata.json.unreadable").write_text('{"earlier": ')
    path.write_text('{"later": ')

    WatchMetadata.load_from_disk(path)

    set_aside = sorted(
        p.read_text() for p in path.parent.iterdir() if p.name != path.name
    )
    assert set_aside == ['{"earlier": ', '{"later": ']
