"""The shown-text checker and the full-text engine agree on terms.

A real Tantivy index (TantivyIndexManager, the server's own schema) holds
one single-line document per sample; for every query word the checker
accepts, the documents it keeps (judging the whole document as shown) are
exactly the documents the engine returns. Words the engine would drop (a
token of 40 bytes or more) are refused instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pytest

from code_indexer.server.services.cidx_meta_access.fulltext_terms import (
    first_visible_match,
    parse_fulltext_terms,
)
from code_indexer.services.tantivy_index_manager import TantivyIndexManager

DOCS: Dict[str, str] = {
    "deva": "abcिxyz",  # vowel sign: alphabetic to the engine
    "ypo": "abcͅdef",  # combining mark: alphabetic to the engine
    "sigma": "ΟΔΟΣ",  # upper-case final sigma
    "roman": "abcⅣdef",  # Roman numeral (letter number)
    "under": "foo_bar",
    "cjk": "支付服务",
    "plain": "alpha beta-gamma",
    "accent": "café olé",
}
WORDS = [
    "abc",
    "xyz",
    "def",
    "abcिxyz",
    "abcͅdef",
    "οδος",  # final sigma
    "οδοσ",
    "ΟΔΟΣ",
    "abcⅣdef",
    "foo",
    "bar",
    "foo_bar",
    "支付服务",
    "beta-gamma",
    "gamma",
    "cafe",
    "café",
    "OLÉ",
]


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory) -> TantivyIndexManager:
    fts = TantivyIndexManager(Path(tmp_path_factory.mktemp("agreement")) / "idx")
    fts.initialize_index(create_new=True)
    for name, text in DOCS.items():
        fts.add_document(
            {
                "path": name,
                "content": text,
                "content_raw": text,
                "identifiers": text.split(),
                "line_start": 1,
                "line_end": 1,
                "language": "md",
            }
        )
    fts.commit()
    return fts


def _engine(index: TantivyIndexManager, word: str) -> List[str]:
    return sorted(str(row["path"]) for row in index.search(word, limit=50))


def _checker(word: str) -> List[str]:
    terms = parse_fulltext_terms(word, 0)
    assert terms is not None, word
    return sorted(
        name
        for name, text in DOCS.items()
        if first_visible_match([text], [text], terms) is not None
    )


@pytest.mark.parametrize("word", WORDS)
def test_checker_keeps_what_the_engine_returns(
    index: TantivyIndexManager, word: str
) -> None:
    assert _checker(word) == _engine(index, word)


@pytest.mark.parametrize(
    "word, accepted",
    [
        ("a" * 39, True),
        ("a" * 40, False),
        ("é" * 19 + "x", True),  # 39 bytes
        ("é" * 20, False),  # 40 bytes
        ("x-" + "b" * 40, False),
    ],
)
def test_tokens_the_engine_drops_are_refused(word: str, accepted: bool) -> None:
    assert (parse_fulltext_terms(word, 0) is not None) is accepted


def _chunk_overlap() -> int:
    from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker

    smallest = min(FixedSizeChunker.MODEL_CHUNK_SIZES.values())
    return int(smallest * FixedSizeChunker.OVERLAP_PERCENTAGE)


@pytest.mark.parametrize("distance", [0, 1])
def test_words_longer_than_the_chunk_overlap_are_refused(distance: int) -> None:
    overlap = _chunk_overlap()
    at_overlap = ("ab-" * overlap)[: overlap - 1] + "c"
    assert len(at_overlap) == overlap
    assert parse_fulltext_terms(at_overlap, distance) is not None
    assert parse_fulltext_terms(at_overlap + "d", distance) is None
