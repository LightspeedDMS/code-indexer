"""Shared helpers for the Issue #1975 / #1999 index-consistency tests: read
branch visibility straight from the real store, and record progress
messages."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Set

from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

# Far above the handful of points these tests create; the helper asserts the
# scroll returned no continuation offset, so nothing is ever truncated.
_SCROLL_LIMIT = 1000


def hidden_branches_by_path(
    indexer: SmartIndexer, store: FilesystemVectorStore
) -> Dict[str, Set[str]]:
    """Union of `hidden_branches` over every content point of each path."""
    collection = store.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )
    points, next_offset = store.scroll_points(
        collection_name=collection,
        filter_conditions={"must": [{"key": "type", "match": {"value": "content"}}]},
        limit=_SCROLL_LIMIT,
        with_payload=True,
        with_vectors=False,
    )
    assert next_offset is None, "scroll did not return every content point"
    hidden: Dict[str, Set[str]] = {}
    for point in points:
        payload = point["payload"]
        hidden.setdefault(payload["path"], set()).update(
            payload.get("hidden_branches", [])
        )
    return hidden


class InfoRecorder:
    """Progress callback that keeps every `info=` message."""

    def __init__(self) -> None:
        self.messages: List[str] = []

    def __call__(self, current: int, total: int, path: Path, **kwargs: Any) -> None:
        info = kwargs.get("info")
        if info:
            self.messages.append(str(info))


def reconciled(recorder: InfoRecorder) -> bool:
    """True when the run performed a disk-vs-database reconcile pass."""
    return any("snapshot of indexed files" in m for m in recorder.messages)
