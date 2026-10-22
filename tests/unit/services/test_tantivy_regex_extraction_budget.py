"""The full-text regex highlight extraction shares one bounded evaluation
budget per search. Hits stay (Tantivy's own engine decided them), without a
highlight."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from code_indexer.services.tantivy_index_manager import TantivyIndexManager

pytestmark = pytest.mark.timeout(60)
DOCS = 200
SLOW_PATTERN = r"(a|a)*b"
LINE = "a" * 24 + "! ab"


@pytest.fixture(scope="module")
def manager(tmp_path_factory: pytest.TempPathFactory) -> TantivyIndexManager:
    index = TantivyIndexManager(Path(tmp_path_factory.mktemp("fts")) / "idx")
    index.initialize_index(create_new=True)
    for n in range(DOCS):
        index.add_document(
            {
                "path": f"f{n:03d}.txt",
                "content": LINE,
                "content_raw": LINE,
                "identifiers": [],
                "line_start": 1,
                "line_end": 1,
                "language": "text",
            }
        )
    index.commit()
    return index


def test_extraction_spends_one_budget_per_search(
    manager: TantivyIndexManager, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    started = time.monotonic()
    results = manager.search(query_text=SLOW_PATTERN, use_regex=True, limit=0)
    assert time.monotonic() - started < 2.5
    assert len(results) == DOCS
    for hit in results:
        assert hit["match_text"] == SLOW_PATTERN and hit["snippet"] == ""
        assert hit["regex_extraction_capped"] is True
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert all(SLOW_PATTERN not in w for w in warnings)


def test_rejected_pattern_is_answered_promptly(
    manager: TantivyIndexManager,
) -> None:
    started = time.monotonic()
    with pytest.raises(ValueError):
        manager.search(query_text=r"(a+)+$", use_regex=True, limit=0)
    assert time.monotonic() - started < 1.0
