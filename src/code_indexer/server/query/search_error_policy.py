"""What a failed search may tell its client.

Every search front door (REST ``/api/query`` and ``/api/query/multi``, MCP
``search_code`` and its omni path) classifies a failure through
``classify_search_error``. It is an allow-list: only errors that describe
the caller's own request keep their text --

* ``SearchRequestError`` (incl. ``SearchRepositoryNotFoundError``),
* request validation errors (``ValueError``, incl. pydantic's),
* an ``HTTPException`` with a 4xx status raised deliberately.

Every other exception answers a fixed public message ("Search timed out"
when a timeout is in its cause chain, else "Search failed"); the caller
logs the full detail at ERROR with ``exc_info``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from fastapi import HTTPException

from code_indexer.server.logging_utils import public_error_message
from code_indexer.server.query.semantic_query_manager import (
    SearchRequestError,
    SemanticQueryError,
)

SEARCH_FAILED = "Search failed"
SEARCH_TIMED_OUT = "Search timed out"
# Bound on the cause chain walked; a cycle or a deep chain never loops.
_MAX_CHAIN = 8


@dataclass(frozen=True)
class SearchErrorOutcome:
    """The client-facing message for a failed search, and its class."""

    message: str
    client_error: bool
    timed_out: bool = False


def _chain(error: BaseException) -> List[BaseException]:
    links: List[BaseException] = []
    current: Optional[BaseException] = error
    while current is not None and len(links) < _MAX_CHAIN and current not in links:
        links.append(current)
        current = current.__cause__ or current.__context__
    return links


def _client_text(error: BaseException) -> Optional[str]:
    from code_indexer.server.services.repo_access_guard import (
        AccessFilteringServiceUnavailableError,
    )

    if isinstance(error, (SearchRequestError, AccessFilteringServiceUnavailableError)):
        return str(error)
    if isinstance(error, HTTPException) and 400 <= error.status_code < 500:
        return str(error.detail)
    if isinstance(error, ValueError) and not isinstance(error, SemanticQueryError):
        return str(error)
    return None


def classify_search_error(error: BaseException) -> SearchErrorOutcome:
    """Classify a failed search for its client-facing response."""
    text = _client_text(error)
    if text is not None:
        return SearchErrorOutcome(message=text, client_error=True)
    if any(isinstance(link, TimeoutError) for link in _chain(error)):
        return SearchErrorOutcome(
            message=public_error_message(SEARCH_TIMED_OUT),
            client_error=False,
            timed_out=True,
        )
    return SearchErrorOutcome(
        message=public_error_message(SEARCH_FAILED), client_error=False
    )
