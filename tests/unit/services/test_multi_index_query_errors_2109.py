"""MultiIndexQueryService reports search failures instead of empty results.

Only a genuine timeout (the coordinator's deadline, or a worker raising
TimeoutError) is a timeout, and it raises a typed error; every other worker
exception propagates. A caller-supplied store window argument merges with the
derived one instead of colliding as a duplicate keyword.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.config import VOYAGE_MULTIMODAL_MODEL
from code_indexer.services import multi_index_query_service as miqs
from code_indexer.services.filtered_window import FILTERED_CANDIDATE_WINDOW
from code_indexer.services.multi_index_query_service import (
    MultiIndexQueryService,
    MultiIndexQueryTimeoutError,
)

CODE_COLLECTION = "voyage-code-3"


def _hit(path: str, score: float) -> Dict[str, Any]:
    return {"score": score, "payload": {"path": path, "chunk_offset": 0}}


class _FakeStore:
    """Vector store double: per-collection outcome (results or exception)."""

    def __init__(self, outcomes: Dict[str, Any]) -> None:
        self.outcomes = outcomes
        self.calls: List[Dict[str, Any]] = []
        self.release = threading.Event()

    def search(self, **kwargs: Any):
        self.calls.append(kwargs)
        outcome = self.outcomes[kwargs["collection_name"]]
        if outcome == "block":
            self.release.wait(timeout=10)
            return [], {}
        if isinstance(outcome, BaseException):
            raise outcome
        return list(outcome), {}


def _service(tmp_path: Path, store: _FakeStore, multimodal: bool = True):
    if multimodal:
        (tmp_path / ".code-indexer" / "index" / VOYAGE_MULTIMODAL_MODEL).mkdir(
            parents=True
        )
    service = MultiIndexQueryService(
        project_root=tmp_path, vector_store=store, embedding_provider=object()
    )
    service._multimodal_providers[VOYAGE_MULTIMODAL_MODEL] = object()
    return service


def test_code_index_error_with_successful_multimodal_sibling_raises(
    tmp_path: Path,
) -> None:
    store = _FakeStore(
        {
            CODE_COLLECTION: RuntimeError("storage read failed"),
            VOYAGE_MULTIMODAL_MODEL: [_hit("docs/a.md", 0.9)],
        }
    )
    with pytest.raises(RuntimeError, match="storage read failed"):
        _service(tmp_path, store).query("q", 5, CODE_COLLECTION)


def test_multimodal_error_with_successful_code_sibling_raises(tmp_path: Path) -> None:
    store = _FakeStore(
        {
            CODE_COLLECTION: [_hit("src/a.py", 0.9)],
            VOYAGE_MULTIMODAL_MODEL: ValueError("bad vector"),
        }
    )
    with pytest.raises(ValueError, match="bad vector"):
        _service(tmp_path, store).query("q", 5, CODE_COLLECTION)


def test_worker_timeout_error_raises_typed_timeout(tmp_path: Path) -> None:
    store = _FakeStore(
        {
            CODE_COLLECTION: TimeoutError("provider timed out"),
            VOYAGE_MULTIMODAL_MODEL: [_hit("docs/a.md", 0.9)],
        }
    )
    with pytest.raises(MultiIndexQueryTimeoutError) as excinfo:
        _service(tmp_path, store).query("q", 5, CODE_COLLECTION)
    assert excinfo.value.index_types == ["code"]


def test_coordinator_deadline_raises_typed_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(miqs, "QUERY_TIMEOUT", 0.2)
    store = _FakeStore(
        {CODE_COLLECTION: "block", VOYAGE_MULTIMODAL_MODEL: [_hit("docs/a.md", 0.9)]}
    )
    service = _service(tmp_path, store)
    timer = threading.Timer(1.0, store.release.set)
    timer.start()
    try:
        with pytest.raises(MultiIndexQueryTimeoutError) as excinfo:
            service.query("q", 5, CODE_COLLECTION)
    finally:
        store.release.set()
        timer.cancel()
    assert excinfo.value.index_types == ["code"]
    assert excinfo.value.timeout_seconds == 0.2


def test_success_still_merges_both_indexes(tmp_path: Path) -> None:
    store = _FakeStore(
        {
            CODE_COLLECTION: [_hit("src/a.py", 0.5)],
            VOYAGE_MULTIMODAL_MODEL: [_hit("docs/a.md", 0.9)],
        }
    )
    results, timing = _service(tmp_path, store).query("q", 5, CODE_COLLECTION)
    assert [r["payload"]["path"] for r in results] == ["docs/a.md", "src/a.py"]
    assert timing["code_timed_out"] is False
    assert timing["multimodal_timed_out"] is False


_FILTER = {"must": [{"key": "language", "match": {"value": "py"}}]}
_DERIVED_PREFETCH = max(FILTERED_CANDIDATE_WINDOW, 2 * 2 * 5)


@pytest.mark.parametrize(
    "extra",
    [
        {"prefetch_limit": _DERIVED_PREFETCH},
        {"lazy_load": True},
        {"prefetch_limit": _DERIVED_PREFETCH, "lazy_load": True},
    ],
)
def test_filtered_caller_window_kwargs_merge_without_duplicate_keyword(
    tmp_path: Path, extra: Dict[str, Any]
) -> None:
    store = _FakeStore({CODE_COLLECTION: [_hit("src/a.py", 0.5)]})
    _service(tmp_path, store, multimodal=False).query(
        "q", 5, CODE_COLLECTION, _FILTER, **extra
    )
    (call,) = store.calls
    assert call["prefetch_limit"] == _DERIVED_PREFETCH
    assert call["lazy_load"] is True


def test_unfiltered_caller_prefetch_limit_is_passed_through(tmp_path: Path) -> None:
    store = _FakeStore({CODE_COLLECTION: [_hit("src/a.py", 0.5)]})
    _service(tmp_path, store, multimodal=False).query(
        "q", 5, CODE_COLLECTION, None, prefetch_limit=7
    )
    (call,) = store.calls
    assert call["prefetch_limit"] == 7
    assert call["limit"] == 10


def test_conflicting_window_kwarg_is_rejected(tmp_path: Path) -> None:
    store = _FakeStore({CODE_COLLECTION: [_hit("src/a.py", 0.5)]})
    with pytest.raises(ValueError, match="prefetch_limit"):
        _service(tmp_path, store, multimodal=False).query(
            "q", 5, CODE_COLLECTION, _FILTER, prefetch_limit=3
        )
    assert store.calls == []
