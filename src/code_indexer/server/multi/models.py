"""
Request and response models for multi-repository search.

Provides Pydantic models for API request validation and response structure.
"""

from typing import Dict, List, Optional, Any, Literal
from pydantic import BaseModel, Field, field_validator


class MultiSearchRequest(BaseModel):
    """
    Request model for multi-repository search.

    Attributes:
        repositories: List of repository identifiers to search
        query: Search query string
        search_type: Type of search (semantic, fts, regex, temporal)
        limit: Maximum results per repository (default: 10)
        min_score: Minimum similarity score (optional, for semantic/FTS)
        language: Filter by programming language (optional)
        path_filter: Filter by file path pattern (optional)
        exclude_language: Exclude files of specified language (optional)
        exclude_path: Exclude files matching path pattern (optional)
        accuracy: Search accuracy profile - fast, balanced, high (optional)
        file_extensions: Keep only files with these extensions (optional,
            semantic/FTS; the #2047 rule of services/extension_filter.py)
    """

    repositories: List[str] = Field(
        ...,
        description="List of repository identifiers to search",
        min_length=1,
    )
    query: str = Field(..., description="Search query string")
    search_type: Literal["semantic", "fts", "regex", "temporal"] = Field(
        ..., description="Type of search to perform"
    )
    limit: int = Field(10, description="Maximum results per repository", ge=1)
    min_score: Optional[float] = Field(
        None, description="Minimum similarity score (semantic/FTS)", ge=0.0, le=1.0
    )
    language: Optional[str] = Field(None, description="Filter by programming language")
    path_filter: Optional[str] = Field(None, description="Filter by file path pattern")
    exclude_language: Optional[str] = Field(
        None, description="Exclude files of specified language"
    )
    exclude_path: Optional[str] = Field(
        None, description="Exclude files matching path pattern"
    )
    accuracy: Optional[str] = Field(
        None, description="Search accuracy profile ('fast', 'balanced', 'high')"
    )
    # #2047: the same rule as single-repository search (semantic and FTS).
    file_extensions: Optional[List[str]] = Field(
        None,
        description=(
            "Keep only files with one of these extensions (e.g., ['py', '.js']). "
            "Case-insensitive, leading dot optional, several values OR-ed; "
            "extensionless files never match; intersected with 'language'."
        ),
    )
    # The #2047 request deadline is server-only: InternalMultiSearchRequest.
    # Story #1108: per-request cache bypass flag — threads through to SemanticSearchRequest
    no_embedding_cache_shortcut: bool = Field(
        False,
        description="Bypass the query-embedding cache for this request (Story #1108)",
    )
    # Story #1291 AC7/AC8: explicit temporal embedder override. Omit to use
    # temporal.active_embedder. An override naming an embedder with no
    # indexed collections returns an empty/typed result -- it never silently
    # falls back to active_embedder. Only meaningful for search_type="temporal".
    temporal_embedder: Optional[str] = Field(
        None,
        description="Explicit temporal embedder override (e.g. 'embed-v4.0'). Only used when search_type='temporal'.",
    )
    # Query vectors are computed by the server: InternalMultiSearchRequest.

    @field_validator("repositories")
    @classmethod
    def validate_repositories(cls, v: List[str]) -> List[str]:
        """Ensure at least one repository is specified."""
        if not v:
            raise ValueError("Must specify at least one repository")
        return v

    @field_validator("file_extensions")
    @classmethod
    def validate_file_extensions(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        """#2047: the one shared file_extensions validation."""
        from code_indexer.services.extension_filter import (
            validate_file_extensions_field,
        )

        return validate_file_extensions_field(v)


class InternalMultiSearchRequest(MultiSearchRequest):
    """Server-only multi-repository request, built by MultiSearchService.search
    from the client's MultiSearchRequest; never a request body, so a client
    cannot set the fields below (an unknown body field is ignored).

    extension_deadline: the #2047 time budget (a time.monotonic() reading)
    the server computes once per request and shares with every repository.

    precomputed_query_vector / precomputed_query_vector_digest: the query
    embedding the server's omni step computes ONCE before the fan-out
    (Story #1148), with the digest of the provider config that produced it;
    each repository reuses the vector only when its own provider digest
    matches. Excluded from serialisation.
    """

    extension_deadline: Optional[float] = None
    precomputed_query_vector: Optional[List[float]] = Field(None, exclude=True)
    precomputed_query_vector_digest: Optional[str] = Field(None, exclude=True)


class MultiSearchMetadata(BaseModel):
    """
    Metadata for multi-repository search response.

    Attributes:
        total_results: Total number of results across all repositories
        total_repos_searched: Number of repositories successfully searched
        execution_time_ms: Total execution time in milliseconds
    """

    total_results: int = Field(..., description="Total number of results")
    total_repos_searched: int = Field(
        ..., description="Repositories successfully searched"
    )
    execution_time_ms: int = Field(..., description="Execution time in milliseconds")


class MultiSearchResponse(BaseModel):
    """
    Response model for multi-repository search.

    Attributes:
        results: Dictionary mapping repository ID to list of search results
        metadata: Search execution metadata
        errors: Optional dictionary mapping repository ID to error message
    """

    results: Dict[str, List[Dict[str, Any]]] = Field(
        ..., description="Search results grouped by repository"
    )
    metadata: MultiSearchMetadata = Field(..., description="Search metadata")
    errors: Optional[Dict[str, str]] = Field(
        None, description="Errors encountered during search"
    )
