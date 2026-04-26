"""
Story #908 AC6: Journal append is atomic per-line under concurrent multi-threaded dispatch.

Tests (2 total):
- test_concurrent_400_appends_all_preserved: asserts count==400, each line valid JSON with 'action'
- test_concurrent_no_interleaved_lines: asserts count==200, each line's source_domain is a str

Marked @pytest.mark.slow: fsync-per-entry under parallel chunk load exceeds the 15s pytest
timeout. Both tests pass when run in isolation; slow marking ensures they are not included
in the parallel server-fast-automation.sh chunks.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

import pytest

from tests.unit.server.services.test_dep_map_908_builders import make_repair_journal
from tests.unit.server.services.test_dep_map_908_entry_builder import make_journal_entry

_THREADS = 4
_APPENDS_PER_THREAD = 100
_TOTAL_APPENDS = _THREADS * _APPENDS_PER_THREAD

_APPENDS_PER_THREAD_SMALL = 50
_TOTAL_APPENDS_SMALL = _THREADS * _APPENDS_PER_THREAD_SMALL

_ENTRY_INDEX_WIDTH = 3


def _append_entries(journal, thread_id: int, count: int) -> None:
    """Append `count` entries to journal from a single thread."""
    for i in range(count):
        entry = make_journal_entry(
            source_domain=f"domain-t{thread_id}-{i:0{_ENTRY_INDEX_WIDTH}d}"
        )
        journal.append(entry)


def _run_concurrent_appends(
    journal_path: Path, threads: int, per_thread: int
) -> List[str]:
    """Create journal, dispatch threads, wait for completion, return file lines."""
    journal = make_repair_journal(journal_path)
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [
            pool.submit(_append_entries, journal, tid, per_thread)
            for tid in range(threads)
        ]
        for f in futures:
            f.result()
    return journal_path.read_text().splitlines()


@pytest.mark.slow
def test_concurrent_400_appends_all_preserved(tmp_path):
    """4 threads x 100 appends = exactly 400 valid JSON lines, each with 'action' key."""
    lines = _run_concurrent_appends(
        tmp_path / "journal.jsonl", _THREADS, _APPENDS_PER_THREAD
    )
    assert len(lines) == _TOTAL_APPENDS, (
        f"Expected {_TOTAL_APPENDS} lines. Got: {len(lines)}"
    )
    for i, line in enumerate(lines):
        assert "action" in json.loads(line), f"Line {i} missing 'action' field"


@pytest.mark.slow
def test_concurrent_no_interleaved_lines(tmp_path):
    """4 threads x 50 appends = exactly 200 lines; each line's source_domain is a str."""
    lines = _run_concurrent_appends(
        tmp_path / "journal.jsonl", _THREADS, _APPENDS_PER_THREAD_SMALL
    )
    assert len(lines) == _TOTAL_APPENDS_SMALL, (
        f"Expected {_TOTAL_APPENDS_SMALL} lines. Got: {len(lines)}"
    )
    for i, line in enumerate(lines):
        parsed = json.loads(line)
        assert isinstance(parsed["source_domain"], str), (
            f"Line {i} has unexpected source_domain type"
        )
