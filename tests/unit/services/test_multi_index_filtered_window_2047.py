"""#2047 (S21): MultiIndexQueryService owns the filtered candidate window.

The service asks the store for ``2 x limit`` results, so the shared rule
``filtered_window_kwargs`` must be applied to THAT store limit, inside the
service, not by callers that only know the outer limit. Replaced: the vector
store (a recording fake at the store boundary) and the embedding providers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from code_indexer.services.multi_index_query_service import MultiIndexQueryService

FILTER = {"must": [{"key": "path", "match": {"any_ext": ["py"]}}]}


class _RecordingStore:
    """Records every store search call; returns no results."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def search(self, **kwargs: Any):
        self.calls.append(kwargs)
        return [], {}


def _service(tmp_path: Path, *, multimodal: bool) -> tuple:
    root = tmp_path / "project"
    (root / ".code-indexer" / "index").mkdir(parents=True)
    if multimodal:
        (root / ".code-indexer" / "index" / "voyage-multimodal-3").mkdir()
    store = _RecordingStore()
    service = MultiIndexQueryService(
        project_root=root, vector_store=store, embedding_provider=object()
    )
    service._multimodal_providers["voyage-multimodal-3"] = object()
    return service, store


def test_filtered_query_window_follows_the_store_limit(tmp_path: Path) -> None:
    service, store = _service(tmp_path, multimodal=True)
    service.query("q", 200, "code_index", FILTER)

    assert len(store.calls) == 2
    for call in store.calls:
        assert call["limit"] == 400
        assert call["prefetch_limit"] == 800
        assert call["lazy_load"] is True


def test_unfiltered_query_carries_no_window_kwargs(tmp_path: Path) -> None:
    service, store = _service(tmp_path, multimodal=True)
    service.query("q", 200, "code_index", None)

    assert len(store.calls) == 2
    for call in store.calls:
        assert call["limit"] == 400
        assert "prefetch_limit" not in call
        assert "lazy_load" not in call


def test_separate_kwargs_query_window_follows_the_store_limit(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path, multimodal=True)
    service.query_with_separate_kwargs(
        "q",
        200,
        "code_index",
        FILTER,
        code_kwargs={"ef": 50},
        multimodal_kwargs={"ef": 50, "no_embedding_cache_shortcut": True},
    )

    assert len(store.calls) == 2
    assert all(c["prefetch_limit"] == 800 for c in store.calls)
    assert all(c["lazy_load"] is True for c in store.calls)


def test_multimodal_only_query_window_follows_the_store_limit(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path, multimodal=True)
    service.query_multimodal_index_only("q", 200, "code_index", FILTER)

    assert len(store.calls) == 1
    assert store.calls[0]["limit"] == 400
    assert store.calls[0]["prefetch_limit"] == 800
    assert store.calls[0]["lazy_load"] is True


def test_small_filtered_query_keeps_the_window_floor(tmp_path: Path) -> None:
    service, store = _service(tmp_path, multimodal=False)
    service.query("q", 10, "code_index", FILTER)

    assert len(store.calls) == 1
    assert store.calls[0]["limit"] == 20
    assert store.calls[0]["prefetch_limit"] == 400
