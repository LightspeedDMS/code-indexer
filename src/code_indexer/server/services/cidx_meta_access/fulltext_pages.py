"""A restricted caller's literal full-text page over the catalog, per document.

The engine indexes one document per CHUNK of RAW text, hidden lines
included, so neither an engine hit nor an engine score may decide what a
restricted caller gets. The page is built from shown text only:

- Candidates. A document the caller is shown as a projection (a
  description or dependency-map file) may show text the engine never
  indexed (a placeholder, a recomputed count), so every such document the
  caller may see is a candidate. A document shown as it is stored is found
  by the engine: every required word is asked for its hits, restricted to
  those documents (``CatalogScope.allowed_paths``), with no snippets,
  doubling the hit count until one word's hits are COMPLETE (the engine
  inspected every match); every such document that shows the words holds
  that word, so its hits cover them all.
- Judging. Each candidate's shown text is judged once per request
  (``CatalogScope.shown_match``); a kept document yields one row built
  from its shown text alone (``fulltext_document_rows``).
- Selection. Kept documents are ranked by how often the required words
  occur in their shown text, then by path; the page is the first
  ``requested_size(limit)`` of them and each row's score is its page rank.

So which documents a page holds, at any limit, every count, and each row's
anchor and snippet are a function of the caller's shown text and the
request. The doubling is bounded by the engine's own hit cap; a word still
incomplete there is refused with a neutral message. Synchronous engine and
file reads: async doors run it on a thread. ``requested_size`` and
``deepening_ks`` also size the single-repository search's semantic rounds.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, FrozenSet, List, Tuple

from .fulltext_terms import FullTextTerms, FulltextOptions, raise_fulltext_too_broad
from .hit_projection import CatalogScope, fulltext_document_rows

if TYPE_CHECKING:
    from ....services.tantivy_index_manager import TantivyIndexManager

META_DEEPEN_MAX_ROUNDS = 4
META_DEEPEN_MAX_K = 1024
# A row's score is its page rank: the first row scores this, each next one
# the step less (the convention of the single-repository full-text rows).
PAGE_RANK_TOP_SCORE = 1.0
PAGE_RANK_STEP = 0.01

# An engine row: heterogeneous values (path, line, text, score) by design.
EngineRow = Dict[str, Any]
# (engine query text, hit count, allowed paths) -> (hits, complete)
SearchWord = Callable[[str, int, FrozenSet[str]], Tuple[List[EngineRow], bool]]


def requested_size(limit: int) -> int:
    """The page size a catalog search serves: *limit*, at least one (the
    single-repository search's rule)."""
    return max(1, limit)


def deepening_ks(limit: int) -> List[int]:
    """The raw hit count of every semantic deepening round for *limit*."""
    requested = requested_size(limit)
    return [
        min(requested * 2 ** (index + 1), META_DEEPEN_MAX_K)
        for index in range(META_DEEPEN_MAX_ROUNDS)
    ]


def _engine_cap() -> int:
    from ....services.tantivy_index_manager import _FTS_UNLIMITED_HITS

    return _FTS_UNLIMITED_HITS


def complete_hits(
    search_word: SearchWord, terms: FullTextTerms, limit: int, allowed: FrozenSet[str]
) -> List[EngineRow]:
    """The hits gathered until one required word's hits are complete.

    Raises:
        SearchParameterError: even the engine's hit cap left every word
            incomplete (the neutral too-broad refusal).
    """
    cap = _engine_cap()
    k = min(2 * requested_size(limit), cap)
    for _ in range(cap.bit_length() + 1):
        hits: List[EngineRow] = []
        for word in terms.engine_queries():
            found, complete = search_word(word, k, allowed)
            hits.extend(found)
            if complete:
                return hits
        if k >= cap:
            break
        k = min(2 * k, cap)
    raise_fulltext_too_broad()


def engine_search_word(
    index: "TantivyIndexManager", options: FulltextOptions, edit_distance: int
) -> SearchWord:
    """*index*'s single-word search with the request's options, without
    snippets (rows are built from shown text afterwards).

    Raises:
        RuntimeError: the engine did not report exactly one completeness
            flag (it appends one whenever ``completeness_out`` is given).
    """

    def search(
        word: str, k: int, allowed: FrozenSet[str]
    ) -> Tuple[List[EngineRow], bool]:
        complete: List[bool] = []
        rows = index.search(
            query_text=word,
            case_sensitive=options.case_sensitive,
            edit_distance=edit_distance,
            snippet_lines=0,
            limit=k,
            use_regex=False,
            allowed_paths=allowed,
            completeness_out=complete,
            **dict(options.filters),
        )
        if len(complete) != 1:
            raise RuntimeError("full-text engine reported no completeness flag")
        return list(rows), complete[0]

    return search


def _ranked(rows: List[EngineRow]) -> List[EngineRow]:
    """*rows* with each one's score set to its page rank."""
    return [
        dict(row, score=PAGE_RANK_TOP_SCORE - PAGE_RANK_STEP * rank)
        for rank, row in enumerate(rows)
    ]


def catalog_fulltext_rows(
    root: Path,
    options: FulltextOptions,
    terms: FullTextTerms,
    scope: CatalogScope,
    limit: int,
    edit_distance: int = 0,
) -> List[EngineRow]:
    """The caller's page of engine-shaped rows over the catalog copy at
    *root*: one per shown document, ranked from shown text (see module
    docstring)."""
    from ...multi.fts_rows import open_fts_index

    allowed = scope.allowed_paths()
    projected = frozenset(path for path in allowed if scope.shows_projection(path))
    stored = allowed - projected
    candidates = set(projected)
    if stored:
        search_word = engine_search_word(open_fts_index(root), options, edit_distance)
        hits = complete_hits(search_word, terms, limit, stored)
        candidates.update(str(hit["path"]) for hit in hits)
    rows = fulltext_document_rows(candidates, scope, terms, options.snippet_lines)
    return _ranked(rows[: requested_size(limit)])
