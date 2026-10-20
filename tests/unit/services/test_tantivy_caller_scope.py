"""TantivyIndexManager.search: caller-scoped paths and completeness.

A real Tantivy index of 30 one-line documents: `allowed_paths` restricts
which stored paths may match (pushed down on the exact-path field, and
re-checked per hit when the index has no such field), and
`completeness_out` reports whether every matching document was inspected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Tuple

import pytest

from code_indexer.services.tantivy_index_manager import TantivyIndexManager

WORD = "zeta"


@pytest.fixture
def index(tmp_path: Path) -> TantivyIndexManager:
    fts = TantivyIndexManager(tmp_path / "idx")
    fts.initialize_index(create_new=True)
    for n in range(30):
        fts.add_document(
            {
                "path": f"docs/p{n:02d}.md",
                "content": f"{WORD} " * (n + 1),
                "content_raw": WORD,
                "identifiers": [WORD],
                "line_start": 1,
                "line_end": 1,
                "language": "md",
            }
        )
    fts.commit()
    return fts


def _search(
    fts: TantivyIndexManager,
    limit: int,
    **options: Any,  # forwarded as-is to search(), whose keywords differ in type
) -> Tuple[List[str], List[bool]]:
    complete: List[bool] = []
    rows = fts.search(WORD, limit=limit, completeness_out=complete, **options)
    return sorted(str(r["path"]) for r in rows), complete


ALLOWED = ["docs/p01.md", "docs/p02.md", "docs/p03.md"]


def test_completeness_reports_whether_every_match_was_inspected(
    index: TantivyIndexManager,
) -> None:
    rows, complete = _search(index, 10)
    assert len(rows) == 10 and complete == [False]
    rows, complete = _search(index, 40)
    assert len(rows) == 30 and complete == [True]
    rows, complete = _search(index, 30)
    assert len(rows) == 30 and complete == [True]


def test_allowed_paths_restrict_matches_and_completeness_reports_exhaustion(
    index: TantivyIndexManager,
) -> None:
    assert _search(index, 5, allowed_paths=ALLOWED) == (ALLOWED, [True])
    assert _search(index, 3, allowed_paths=ALLOWED) == (ALLOWED, [True])
    rows, complete = _search(index, 2, allowed_paths=ALLOWED)
    assert len(rows) == 2 and set(rows) <= set(ALLOWED) and complete == [False]
    assert _search(index, 5, allowed_paths=[]) == ([], [True])


def test_allowed_paths_hold_without_the_exact_path_field(
    index: TantivyIndexManager,
) -> None:
    index._exact_path_field_available = False  # an index built before the field
    rows, complete = _search(index, 40, allowed_paths=ALLOWED)
    assert rows == ALLOWED and complete == [True]
    rows, complete = _search(index, 3, allowed_paths=ALLOWED)
    # Without the push-down, 3 raw hits cannot cover all 30 matches.
    assert set(rows) <= set(ALLOWED) and complete == [False]


def test_post_filtered_search_is_complete_only_when_every_match_was_fetched(
    index: TantivyIndexManager,
) -> None:
    # 2 x 3 raw hits are fetched: the top scorers, none of them under p0*.
    rows, complete = _search(index, 2, path_filters=["*p0*"])
    assert all("/p0" in row for row in rows) and complete == [False]
    rows, complete = _search(index, 40, path_filters=["*p0*"])
    assert len(rows) == 10 and complete == [True]


def test_search_without_the_options_is_unchanged(index: TantivyIndexManager) -> None:
    plain = index.search(WORD, limit=7)
    scoped, _ = _search(index, 7)
    assert sorted(str(r["path"]) for r in plain) == scoped
