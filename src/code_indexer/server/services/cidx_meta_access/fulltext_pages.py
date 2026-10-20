"""A restricted caller's literal full-text page over the catalog, per document.

The engine indexes one document per CHUNK, so a chunk hit says nothing
about what the caller sees of its file, and a file whose words sit in
different chunks has no hit for the whole query at all. The page is
therefore built per file: one single-word engine query per required word
gathers candidate files (the union); each candidate's shown text is judged
once per request (``CatalogScope.shown_match``); a kept file yields exactly
one row (``project_fulltext_documents``). Which files appear, and every
count, depend only on the text the caller is shown; the order follows the
engine.

Bounded deepening: round r asks every word for ``limit * 2**(r + 1)`` hits
(capped at ``META_DEEPEN_MAX_K``), at most ``META_DEEPEN_MAX_ROUNDS``
rounds, stopping early when the page is full, every word's hits are
exhausted, or the request's search budget is spent. Constants, not
settings. Synchronous engine and file reads: async doors run it on a
thread.
"""

from __future__ import annotations

from typing import Any, Callable, List, Tuple

from .fulltext_terms import FullTextTerms
from .hit_projection import CatalogScope, project_fulltext_documents

META_DEEPEN_MAX_ROUNDS = 4
META_DEEPEN_MAX_K = 1024

# (engine query text, hit count) -> the engine's hits for it, best first.
SearchWord = Callable[[str, int], List[Any]]


def requested_size(limit: int) -> int:
    """The page size a catalog search serves: *limit*, at least one (the
    single-repository search's rule)."""
    return max(1, limit)


def deepening_ks(limit: int) -> List[int]:
    """The raw hit count of every deepening round for a page of *limit*."""
    requested = requested_size(limit)
    return [
        min(requested * 2 ** (index + 1), META_DEEPEN_MAX_K)
        for index in range(META_DEEPEN_MAX_ROUNDS)
    ]


def word_hits(
    search_word: SearchWord, terms: FullTextTerms, k: int
) -> Tuple[List[Any], bool]:
    """Every required word's top *k* engine hits, and whether every word's
    hits are exhausted (fewer than *k*)."""
    hits: List[Any] = []
    exhausted = True
    for word in terms.engine_queries():
        found = search_word(word, k)
        hits.extend(found)
        exhausted = exhausted and len(found) < k
    return hits, exhausted


def fulltext_page(
    search_word: SearchWord,
    scope: Callable[[], CatalogScope],
    terms: FullTextTerms,
    limit: int,
    budget_spent: Callable[[], bool],
) -> List[Any]:
    """The caller's first documents (``requested_size(limit)``), one row
    each, best first. *scope* is called after the first engine query ran,
    so a search error surfaces unchanged."""
    page: List[Any] = []
    for k in deepening_ks(limit):
        hits, exhausted = word_hits(search_word, terms, k)
        page = project_fulltext_documents(hits, scope(), terms)
        if len(page) >= requested_size(limit) or exhausted:
            break
        if k >= META_DEEPEN_MAX_K or budget_spent():
            break
    return page[: requested_size(limit)]
