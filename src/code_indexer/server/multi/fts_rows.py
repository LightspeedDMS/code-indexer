"""Full-text rows of one repository for a multi-repository search request.

Shared by the multi-search worker (``MultiSearchService._search_fts_sync``)
and the restricted catalog page, which opens the index once per request and
asks it one word at a time with the same request filters.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..query.filtered_search import fts_filter_kwargs

if TYPE_CHECKING:
    from ...services.tantivy_index_manager import TantivyIndexManager

MULTI_FTS_SNIPPET_LINES = 3


def open_fts_index(repo_path: Path) -> "TantivyIndexManager":
    """The repository's full-text index, opened read-only (no writer and no
    lock are taken, so nothing needs releasing).

    Raises:
        FileNotFoundError: the repository has no full-text index.
    """
    from ...services.tantivy_index_manager import TantivyIndexManager

    fts_index_dir = Path(repo_path) / ".code-indexer" / "tantivy_index"
    if not fts_index_dir.exists():
        raise FileNotFoundError(f"FTS index not found at {fts_index_dir}")
    index = TantivyIndexManager(fts_index_dir)
    index.open_for_search()
    return index


def multi_fts_filters(request: Any, deadline: Optional[float]) -> Dict[str, Any]:
    """*request*'s filters as engine keyword arguments (#2047
    file_extensions under *deadline*)."""
    return fts_filter_kwargs(
        language=request.language,
        path_filter=request.path_filter,
        exclude_language=request.exclude_language,
        exclude_path=request.exclude_path,
        file_extensions=request.file_extensions,
        deadline=deadline,
    )


def multi_row(fts_result: Dict[str, Any]) -> Dict[str, Any]:
    """One engine row in the multi-search row shape."""
    return {
        "file_path": fts_result.get("path", ""),
        "line_start": fts_result.get("line", 0),
        "line_end": fts_result.get("line", 0),
        "score": fts_result.get("score", 0.0),
        "content": fts_result.get("match_text", ""),
        "language": fts_result.get("language", ""),
    }


def fts_search_rows(
    index: "TantivyIndexManager",
    request: Any,
    query: str,
    limit: int,
    deadline: Optional[float],
) -> List[Dict[str, Any]]:
    """The top *limit* full-text rows for *query* in *index*, with
    *request*'s filters, in the multi-search row shape."""
    fts_results = index.search(
        query_text=query,
        limit=limit,
        use_regex=False,
        snippet_lines=MULTI_FTS_SNIPPET_LINES,
        **multi_fts_filters(request, deadline),
    )
    return [multi_row(fts_result) for fts_result in fts_results]
