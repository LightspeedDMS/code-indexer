"""The candidate window of a FILTERED semantic query (#2047).

One rule for the standalone CLI, the daemon and the server, so a selective
filter (language, path, exclude_language, exclude_path, file_extensions)
fills the same limit in every mode. Pure Python, import-light: the CLI
imports it without pulling in any server module.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# = 2 x the server's MAX_CANDIDATE_LIMIT (200, server/models/api_models.py).
FILTERED_CANDIDATE_WINDOW = 400


def filtered_window_kwargs(
    filter_conditions: Optional[Dict[str, Any]], limit: int
) -> Dict[str, Any]:
    """FilesystemVectorStore.search kwargs for a semantic query asking the
    store for *limit* results under *filter_conditions*.

    Filtered: the store reads up to ``max(FILTERED_CANDIDATE_WINDOW,
    2 x limit)`` HNSW candidates in similarity order and stops once *limit*
    match (``lazy_load``), so ONE store query fills a selective filter.
    RECALL RULE: exact whenever the top *limit* matches lie inside that
    window; otherwise fewer results, never a non-matching one.

    Unfiltered: empty, so the store's default window is unchanged.
    """
    if not filter_conditions:
        return {}
    return {
        "prefetch_limit": max(FILTERED_CANDIDATE_WINDOW, 2 * limit),
        "lazy_load": True,
    }
