"""Unit tests for Bug #1971: `SmartIndexer._scroll_all_content_points`
(`src/code_indexer/services/smart_indexer.py`, around lines 3279-3322)
stops after the FIRST page (5000 points) of any collection, because its
"stuck pagination" safety check compares the cursor with itself:

```python
offset = next_offset
if offset is None:
    break
if next_offset == offset:   # always True -- offset WAS just set to next_offset
    logger.error(f"Pagination stuck at offset {offset} - breaking")
    break
```

The sibling loop, `HighThroughputProcessor._fetch_all_content_points`
(`src/code_indexer/services/high_throughput_processor.py`, around line
1479), does this correctly: it compares `next_offset == offset` BEFORE
reassigning `offset`, so it only detects a GENUINELY repeated cursor, not
every ordinary page advance.

These tests drive `SmartIndexer._scroll_all_content_points` directly
against a small, deterministic, real-pagination in-memory fake
`vector_store_client` (an "in-memory implementation" per the mocking
hierarchy, not a Mock/MagicMock of the code under test) that honors
`limit`/`offset` exactly the way the real `FilesystemVectorStore.
scroll_points` contract does: return up to `limit` points and a
resume cursor, or `None` when exhausted. No git or embedding-provider
behavior is exercised, so a lightweight `MagicMock()` stands in for the
embedding provider (an external-service boundary, not the code under
test), matching the existing pattern in
`test_reconcile_batch_content_id_1505.py`.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer


class PaginatingFakeVectorStoreClient:
    """Real, deterministic multi-page pagination: honors `limit`, returns a
    resume cursor (the next start index, as a string) while more points
    remain, and `None` once exhausted -- exactly the
    `FilesystemVectorStore.scroll_points` contract."""

    def __init__(self, points: List[Dict[str, Any]]) -> None:
        self.points = points
        self.scroll_calls = 0

    def scroll_points(
        self,
        collection_name: str,
        filter_conditions: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        offset: Optional[str] = None,
        with_payload: bool = True,
        with_vectors: bool = False,
        subdirectory: Optional[str] = None,
        *,
        self_heal: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        self.scroll_calls += 1
        start = int(offset) if offset is not None else 0
        end = start + limit
        page = self.points[start:end]
        next_offset = str(end) if end < len(self.points) else None
        return page, next_offset


class StuckCursorAfterFirstPageFakeVectorStoreClient:
    """Simulates a genuinely broken backend: the FIRST call legitimately
    advances the cursor (a real, different cursor), but every call after
    that returns the SAME cursor forever -- the one situation the "stuck
    pagination" safety net exists to catch."""

    def __init__(self, points: List[Dict[str, Any]]) -> None:
        self.points = points
        self.scroll_calls = 0

    def scroll_points(
        self,
        collection_name: str,
        filter_conditions: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        offset: Optional[str] = None,
        with_payload: bool = True,
        with_vectors: bool = False,
        subdirectory: Optional[str] = None,
        *,
        self_heal: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        self.scroll_calls += 1
        if offset is None:
            # First page: a real, legitimately advancing cursor.
            return self.points[0:1], "cursor-A"
        # Every subsequent page: the backend bug -- same cursor forever.
        return self.points[1:2], "cursor-A"


def _make_indexer(codebase_dir: Path, vector_store_client: Any) -> SmartIndexer:
    config = Config(codebase_dir=codebase_dir)
    embedding_provider = MagicMock()
    metadata_path = codebase_dir.parent / "metadata.json"
    return SmartIndexer(
        config=config,
        embedding_provider=embedding_provider,
        vector_store_client=vector_store_client,
        metadata_path=metadata_path,
    )


def test_scroll_all_content_points_returns_every_point_across_multiple_pages(
    tmp_path: Path, caplog
) -> None:
    # More than one page at the hardcoded limit=5000 page size.
    total_points = 5001
    points = [
        {
            "id": f"file_{i}.py:0",
            "payload": {"type": "content", "path": f"file_{i}.py"},
        }
        for i in range(total_points)
    ]
    client = PaginatingFakeVectorStoreClient(points)
    indexer = _make_indexer(tmp_path, client)

    with caplog.at_level(logging.ERROR):
        result = indexer._scroll_all_content_points("test_collection")

    assert len(result) == total_points, (
        f"expected the full {total_points}-point snapshot, got "
        f"{len(result)} -- pagination stopped early"
    )
    assert client.scroll_calls == 2, (
        f"expected exactly 2 real pages (5000 + 1), got {client.scroll_calls} "
        "scroll_points calls"
    )
    assert not any("stuck" in record.message.lower() for record in caplog.records), (
        f"unexpected 'stuck' ERROR on a normally-advancing multi-page scroll: {caplog.records}"
    )


def test_scroll_all_content_points_stops_on_genuinely_repeated_cursor(
    tmp_path: Path, caplog
) -> None:
    points = [
        {"id": "a.py:0", "payload": {"type": "content", "path": "a.py"}},
        {"id": "b.py:0", "payload": {"type": "content", "path": "b.py"}},
    ]
    client = StuckCursorAfterFirstPageFakeVectorStoreClient(points)
    indexer = _make_indexer(tmp_path, client)

    with caplog.at_level(logging.ERROR):
        result = indexer._scroll_all_content_points("test_collection")

    # The loop must progress past the FIRST (legitimately advancing) page
    # before detecting the genuinely repeated cursor on the second call --
    # proving the safety net still works, and is bounded (never loops
    # forever retrying the same stuck cursor).
    assert client.scroll_calls == 2, (
        "expected the loop to take the first real page, then detect the "
        f"repeated cursor on page 2 and stop -- got {client.scroll_calls} calls"
    )
    assert len(result) == 2, "expected both pages' points collected before stopping"
    assert any("stuck" in record.message.lower() for record in caplog.records), (
        "expected a 'stuck' ERROR for the genuinely repeated cursor"
    )
