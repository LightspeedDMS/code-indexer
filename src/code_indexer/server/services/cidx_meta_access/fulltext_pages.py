"""A restricted caller's literal full-text page over the catalog, per document.

The engine indexes one document per CHUNK and scores chunks on their raw
text, hidden lines included, so neither a chunk hit nor an engine score may
decide what a restricted caller gets. The page is built per file, from
shown text only:

- Candidates: every required word is asked for its hits, restricted to
  the files the caller may see (``CatalogScope.allowed_paths``, pushed
  down to the engine), doubling the hit count until one word's hits are
  COMPLETE (the engine inspected every match). Every qualifying file shows
  that word, so its hits then cover every qualifying file.
- Judging: each candidate's shown text is judged once per request
  (``CatalogScope.shown_match``); a kept file yields one row, anchored at
  its first shown occurrence.
- Selection: kept files are ranked by how often the required words occur
  in their shown text, then by path, and the page is the first
  ``requested_size(limit)`` of them; each row's score is its page rank.

So which files a page holds, at any limit, and every count are a function
of the caller's shown text. The doubling is bounded by the engine's own hit
cap; a word that is still incomplete there fails the request loudly.
Synchronous engine and file reads: async doors run it on a thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Tuple

from .fulltext_terms import FullTextTerms, FulltextOptions
from .hit_projection import CatalogScope, project_fulltext_documents

META_DEEPEN_MAX_ROUNDS = 4
META_DEEPEN_MAX_K = 1024

# (engine query text, hit count, allowed paths) -> (hits, complete)
SearchWord = Callable[[str, int, FrozenSet[str]], Tuple[List[Any], bool]]


class CatalogFulltextIncomplete(RuntimeError):
    """No required word's hits could be gathered completely."""


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
) -> List[Any]:
    """The hits gathered until one required word's hits are complete.

    Raises:
        CatalogFulltextIncomplete: even the engine's hit cap left every
            word incomplete.
    """
    cap = _engine_cap()
    k = min(2 * requested_size(limit), cap)
    for _ in range(cap.bit_length() + 1):
        hits: List[Any] = []
        for word in terms.engine_queries():
            found, complete = search_word(word, k, allowed)
            hits.extend(found)
            if complete:
                return hits
        if k >= cap:
            break
        k = min(2 * k, cap)
    raise CatalogFulltextIncomplete("catalog full-text candidates exceed the cap")


def engine_search_word(
    index: Any, options: FulltextOptions, edit_distance: int
) -> SearchWord:
    """*index*'s single-word search with the request's options.

    Raises:
        RuntimeError: the engine did not report exactly one completeness
            flag (it appends one whenever ``completeness_out`` is given).
    """

    def search(word: str, k: int, allowed: FrozenSet[str]) -> Tuple[List[Any], bool]:
        complete: List[bool] = []
        rows = index.search(
            query_text=word,
            case_sensitive=options.case_sensitive,
            edit_distance=edit_distance,
            snippet_lines=options.snippet_lines,
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


def _ranked(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """*rows* with each engine score replaced by its page rank."""
    return [dict(row, score=1.0 - 0.01 * rank) for rank, row in enumerate(rows)]


def catalog_fulltext_rows(
    root: Path,
    options: FulltextOptions,
    terms: FullTextTerms,
    scope: CatalogScope,
    limit: int,
    edit_distance: int = 0,
) -> List[Dict[str, Any]]:
    """The caller's page of engine rows over the catalog copy at *root*:
    one per shown document, ranked from shown text (see module docstring)."""
    from ...multi.fts_rows import open_fts_index

    search_word = engine_search_word(open_fts_index(root), options, edit_distance)
    hits = complete_hits(search_word, terms, limit, scope.allowed_paths())
    kept = project_fulltext_documents(hits, scope, terms)
    return _ranked(kept[: requested_size(limit)])
