"""The search seam: run FTS / semantic / hybrid queries somewhere else.

Entry-point group ``code_indexer.search_backend``. ``local`` means the core's
own query engine over this pod's indexes (no backend object is built);
``delegated`` hands queries to a plugin. Query stays the core's product: this
seam only moves WHERE the indexes are read.

Moved from ``code_indexer.server.startup.search_backend_wiring``, which
re-exports it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol


class SearchBackend(Protocol):
    """Protocol for a delegated search backend.

    A plugin-supplied object that can execute FTS, semantic and hybrid queries
    against a remote service. The exact transport is the plugin's concern; cidx
    only calls ``search`` and expects a list of result dicts.
    """

    def search(
        self,
        repository_alias: str,
        query_text: str,
        search_mode: str,  # "fts", "semantic", "hybrid"
        *,
        limit: int = 50,
        min_score: Optional[float] = None,
        language: Optional[str] = None,
        exclude_language: Optional[str] = None,
        path_filter: Optional[str] = None,
        exclude_path: Optional[str] = None,
        case_sensitive: bool = False,
        fuzzy: bool = False,
        edit_distance: int = 0,
        snippet_lines: int = 5,
        regex: bool = False,
        accuracy: str = "balanced",
        include_source: bool = True,
    ) -> List[Dict[str, Any]]: ...
