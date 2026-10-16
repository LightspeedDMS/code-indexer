"""#2047: TantivyIndexManager.search with ``file_extensions``, on a real index.

* The answer fills ``limit`` while matching documents exist, even when many
  higher-ranked documents fail the suffix re-check (extensionless files are
  stored with language ``txt``) or when a value cannot be pushed down into
  the index (``c++``, ``foo-bar``). Expected = full unfiltered scan, filtered
  by the rule, top ``limit``. Values are passed upper-case on purpose: the
  rule is case-insensitive.
* Regression: a language NAME (``python``) filters by the stored suffix
  facets of its extensions (the facet stores ``/py``, never ``/python``).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, List, Sequence, Tuple

import pytest

from code_indexer.services.tantivy_index_manager import (
    _FTS_FILL_HITS_PER_RESULT,
    _FTS_UNLIMITED_HITS,
    TantivyIndexManager,
)

MANAGER_LOGGER = "code_indexer.services.tantivy_index_manager"
LIMIT = 3
DECOYS = 20
TARGET_PREFIX = "t/"


def _build(root: Path, docs: Sequence[Tuple[str, str, str]]) -> Path:
    index_dir = root / "tantivy_index"
    manager = TantivyIndexManager(index_dir)
    manager.initialize_index(create_new=True)
    try:
        for path, language, text in docs:
            manager.add_document(
                {
                    "path": path,
                    "content": text,
                    "content_raw": text,
                    "identifiers": text.split(),
                    "line_start": 1,
                    "line_end": 1,
                    "language": language,
                }
            )
        manager.commit()
    finally:
        manager.close()
    return index_dir


def _search(index_dir: Path, **kwargs) -> List[str]:
    manager = TantivyIndexManager(index_dir)
    manager.open_for_search()
    try:
        return [r["path"] for r in manager.search("widget", snippet_lines=0, **kwargs)]
    finally:
        manager.close()


def _corpus(decoy_path: str, decoy_language: str, target_ext: str):
    """DECOYS high-ranked decoys (query term 3x), then 4 targets."""
    docs = [
        (decoy_path.format(i), decoy_language, "widget widget widget " + "pad " * i)
        for i in range(DECOYS)
    ]
    docs += [
        (
            f"{TARGET_PREFIX}target{i}.{target_ext}",
            target_ext,
            "widget " + "pad " * (DECOYS + i),
        )
        for i in range(4)
    ]
    return docs


@pytest.mark.parametrize(
    "decoy_path, decoy_language, target_ext",
    [
        pytest.param("bin/tool{}", "txt", "txt", id="extensionless-heavy-txt"),
        pytest.param("src/m{}.c", "c", "c++", id="not-pushable-c++"),
        pytest.param("src/m{}.foo", "foo", "foo-bar", id="not-pushable-foo-bar"),
    ],
)
def test_filtered_fts_fills_the_limit(
    tmp_path, decoy_path, decoy_language, target_ext
) -> None:
    index_dir = _build(tmp_path, _corpus(decoy_path, decoy_language, target_ext))
    scan = _search(index_dir, limit=0)
    # Every decoy outranks every target, so the 3x over-fetch cannot reach one.
    assert not any(p.startswith(TARGET_PREFIX) for p in scan[:DECOYS]), scan
    expected = [p for p in scan if p.startswith(TARGET_PREFIX)][:LIMIT]

    got = _search(index_dir, limit=LIMIT, file_extensions=[target_ext.upper()])

    assert got == expected


class _CountingSearcher:
    """Pass-through wrapper over the real Tantivy searcher recording every
    stored-document fetch (hydration) and every search page requested."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.hydrated: List[Tuple[int, int]] = []
        self.pages: List[Tuple[int, int]] = []

    def search(self, query: Any, limit: int = 10, **kwargs: Any) -> Any:
        self.pages.append((limit, kwargs.get("offset", 0)))
        return self._real.search(query, limit, **kwargs)

    def doc(self, address: Any) -> Any:
        self.hydrated.append((address.segment_ord, address.doc))
        return self._real.doc(address)


class _CountingIndex:
    """Pass-through wrapper over the real Tantivy index handing out
    _CountingSearcher instances."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.searchers: List[_CountingSearcher] = []

    def searcher(self) -> _CountingSearcher:
        searcher = _CountingSearcher(self._real.searcher())
        self.searchers.append(searcher)
        return searcher

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _counted_search(index_dir: Path, **kwargs) -> Tuple[List[str], _CountingSearcher]:
    manager = TantivyIndexManager(index_dir)
    manager.open_for_search()
    counting = _CountingIndex(manager._index)
    # Pass-through proxy over the real index (records doc()/search() calls).
    manager._index = counting  # type: ignore[assignment]
    try:
        paths = [r["path"] for r in manager.search("widget", snippet_lines=0, **kwargs)]
    finally:
        manager._index = counting._real
        manager.close()
    (searcher,) = counting.searchers
    return paths, searcher


def _non_matching_corpus(count: int):
    return [(f"src/m{i}.c", "c", "widget " + "pad " * i) for i in range(count)]


def test_refetch_work_is_bounded_and_never_rehydrates(tmp_path, caplog) -> None:
    index_dir = _build(tmp_path, _non_matching_corpus(400))
    with caplog.at_level(logging.INFO, logger=MANAGER_LOGGER):
        got, searcher = _counted_search(index_dir, limit=LIMIT, file_extensions=["c++"])

    assert got == []
    assert len(searcher.hydrated) == len(set(searcher.hydrated))  # never twice
    assert len(searcher.hydrated) == LIMIT * _FTS_FILL_HITS_PER_RESULT
    assert any(
        "per-repository hit bound" in r.getMessage()
        and f"inspecting {LIMIT * _FTS_FILL_HITS_PER_RESULT} hits" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_expired_deadline_stops_refetch(tmp_path, caplog) -> None:
    index_dir = _build(tmp_path, _non_matching_corpus(400))
    with caplog.at_level(logging.INFO, logger=MANAGER_LOGGER):
        got, searcher = _counted_search(
            index_dir,
            limit=LIMIT,
            file_extensions=["c++"],
            deadline=time.monotonic() - 1.0,
        )

    assert got == []
    assert len(searcher.pages) == 1
    assert len(searcher.hydrated) == 3 * LIMIT  # the first page only
    assert any("search time budget" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"limit": 40000, "file_extensions": ["c"]}, id="filtered"),
        pytest.param({"limit": 200000}, id="unfiltered"),
    ],
)
def test_every_fetch_is_clamped_to_the_cap(tmp_path, kwargs) -> None:
    index_dir = _build(tmp_path, _non_matching_corpus(5))
    got, searcher = _counted_search(index_dir, **kwargs)

    assert len(got) == 5
    assert searcher.pages
    assert all(
        offset + size <= _FTS_UNLIMITED_HITS for size, offset in searcher.pages
    ), searcher.pages


@pytest.mark.parametrize(
    "kwargs", [{"language_filter": "python"}, {"languages": ["python"]}]
)
def test_language_name_filters_by_stored_suffix_facets(tmp_path, kwargs) -> None:
    index_dir = _build(
        tmp_path,
        [
            ("a.py", "py", "widget"),
            ("b.pyi", "pyi", "widget"),
            ("C.PY", "PY", "widget"),
            ("d.md", "md", "widget"),
        ],
    )
    assert sorted(_search(index_dir, limit=10, **kwargs)) == ["a.py", "b.pyi"]
