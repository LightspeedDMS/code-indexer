"""
Query Pydantic models for CIDX Server API.

Extracted from app.py as part of Story #409 (app.py modularization).
"""

import datetime as _datetime_module
from typing import List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator, model_validator

from .api_models import QueryResultItem


class SemanticQueryRequest(BaseModel):
    """Request model for semantic query operations with FTS support."""

    query_text: str = Field(
        ..., min_length=1, max_length=1000, description="Natural language query text"
    )
    repository_alias: Optional[str] = Field(
        None, max_length=255, description="Specific repository to search (optional)"
    )
    limit: int = Field(
        default=10, ge=1, le=100, description="Maximum number of results to return"
    )
    min_score: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Minimum similarity score threshold"
    )
    file_extensions: Optional[List[str]] = Field(
        None,
        description="Filter results to specific file extensions (e.g., ['.py', '.js'])",
    )
    async_query: bool = Field(
        default=False, description="Submit as background job if True"
    )

    # Search mode selection (Story 5)
    search_mode: Literal["semantic", "fts", "hybrid"] = Field(
        default="semantic",
        description="Search mode: 'semantic' (AI-based, default), 'fts' (full-text), 'hybrid' (both in parallel)",
    )

    # FTS-specific parameters (Story 5)
    case_sensitive: bool = Field(
        default=False, description="FTS only: Enable case-sensitive matching"
    )
    fuzzy: bool = Field(
        default=False, description="FTS only: Enable fuzzy matching (edit distance 1)"
    )
    edit_distance: int = Field(
        default=0,
        ge=0,
        le=3,
        description="FTS only: Fuzzy match tolerance (0=exact, 1-3=typo tolerance)",
    )
    snippet_lines: int = Field(
        default=5,
        ge=0,
        le=50,
        description="FTS only: Context lines around matches (0=list only, default=5)",
    )

    # Common filtering parameters
    language: Optional[str] = Field(
        None,
        description="Filter by programming language (e.g., 'python', 'javascript')",
    )
    path_filter: Optional[str] = Field(
        None, description="Filter by path pattern (e.g., '*/tests/*', '*.py')"
    )

    # Exclusion filters (Story #503 Phase 1)
    exclude_language: Optional[str] = Field(
        None,
        description="Exclude files of specified language (e.g., 'python', 'javascript')",
    )
    exclude_path: Optional[str] = Field(
        None,
        description="Exclude files matching path pattern (e.g., '*/tests/*', '*.min.js')",
    )

    # Accuracy profile (Story #503 Phase 1)
    accuracy: Literal["fast", "balanced", "high"] = Field(
        default="balanced",
        description="Search accuracy profile: 'fast' (quick), 'balanced' (default), 'high' (thorough)",
    )

    # FTS regex mode (Story #503 Phase 1)
    regex: bool = Field(
        default=False,
        description="FTS only: Interpret query as regex pattern (requires search_mode='fts' or 'hybrid')",
    )

    # Temporal query parameters (Story #446)
    time_range: Optional[str] = Field(
        None,
        description="Time range filter (e.g., '2024-01-01..2024-12-31'). Requires temporal index.",
    )
    time_range_all: bool = Field(
        False,
        description="Query across all git history without time range limit. Requires temporal index.",
    )
    at_commit: Optional[str] = Field(
        None,
        description="Query code at specific commit hash or ref. Requires temporal index.",
    )
    include_removed: bool = Field(
        False,
        description="Include files removed from current HEAD. Requires temporal index.",
    )
    show_evolution: bool = Field(
        False,
        description="Show code evolution timeline with diffs. Requires temporal index.",
    )
    evolution_limit: Optional[int] = Field(
        None, ge=1, description="Limit number of evolution entries. User-controlled."
    )

    # Temporal filtering parameters (Story #503 Phase 3)
    diff_type: Optional[Union[str, List[str]]] = Field(
        None,
        description="Filter temporal results by diff type (added/modified/deleted/renamed/binary). Can be single value or array.",
    )
    author: Optional[str] = Field(
        None,
        description="Filter temporal results by commit author (name or email).",
    )
    chunk_type: Optional[Literal["commit_message", "commit_diff"]] = Field(
        None,
        description="Filter temporal results by chunk type (commit_message or commit_diff).",
    )

    # Omni-search parameters (Story #521)
    aggregation_mode: Optional[str] = Field(
        default="global",
        description="Result aggregation: 'global' returns top-K by score, 'per_repo' samples proportionally",
    )
    exclude_patterns: Optional[List[str]] = Field(
        default=None,
        description="Regex patterns to exclude repositories from omni-search",
    )

    @field_validator("query_text")
    @classmethod
    def validate_query_text(cls, v: str) -> str:
        """Validate query text is not empty or whitespace-only."""
        if not v or not v.strip():
            raise ValueError("Query text cannot be empty or contain only whitespace")
        return v.strip()

    @field_validator("repository_alias")
    @classmethod
    def validate_repository_alias(cls, v: Optional[str]) -> Optional[str]:
        """Validate repository alias if provided."""
        if v is not None and (not v or not v.strip()):
            raise ValueError(
                "Repository alias cannot be empty or contain only whitespace"
            )
        return v.strip() if v else None

    @field_validator("file_extensions")
    @classmethod
    def validate_file_extensions(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        """Validate file extensions format and convert empty list to None."""
        if v is None:
            return None

        if len(v) == 0:
            return None  # Convert empty list to None (no filtering)

        validated_extensions = []
        for ext in v:
            if not ext or not ext.strip():
                raise ValueError(
                    "File extensions cannot be empty or contain only whitespace"
                )

            ext = ext.strip()

            # Must start with dot
            if not ext.startswith("."):
                raise ValueError(f"File extensions must start with dot: {ext}")

            # Must contain only alphanumeric characters after the dot
            if len(ext) <= 1:
                raise ValueError(f"File extensions must have content after dot: {ext}")

            extension_part = ext[1:]  # Remove the dot
            if not extension_part.replace("_", "").replace("-", "").isalnum():
                raise ValueError(
                    f"File extensions must contain only alphanumeric characters, hyphens, and underscores: {ext}"
                )

            validated_extensions.append(ext)

        return validated_extensions

    @field_validator("time_range")
    @classmethod
    def validate_time_range(cls, v: Optional[str]) -> Optional[str]:
        """Validate time_range format (YYYY-MM-DD..YYYY-MM-DD)."""
        if v is None:
            return None

        # Validate format
        if ".." not in v:
            raise ValueError("time_range must be in format 'YYYY-MM-DD..YYYY-MM-DD'")

        parts = v.split("..")
        if len(parts) != 2:
            raise ValueError("time_range must be in format 'YYYY-MM-DD..YYYY-MM-DD'")

        # Validate each date
        for date_str in parts:
            try:
                _datetime_module.datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError as e:
                raise ValueError(
                    f"time_range contains invalid date '{date_str}': {str(e)}"
                )

        return v

    @field_validator("diff_type")
    @classmethod
    def validate_diff_type(
        cls, v: Optional[Union[str, List[str]]]
    ) -> Optional[Union[str, List[str]]]:
        """Validate diff_type values (Story #503 Phase 3)."""
        if v is None:
            return None

        valid_values = {"added", "modified", "deleted", "renamed", "binary"}

        # Handle single string value
        if isinstance(v, str):
            if v not in valid_values:
                raise ValueError(
                    f"diff_type value '{v}' is not valid. Must be one of: {', '.join(sorted(valid_values))}"
                )
            return v

        # Handle list of values
        if isinstance(v, list):
            for value in v:
                if value not in valid_values:
                    raise ValueError(
                        f"diff_type value '{value}' is not valid. Must be one of: {', '.join(sorted(valid_values))}"
                    )
            return v

        return v

    @field_validator("author")
    @classmethod
    def validate_author(cls, v: Optional[str]) -> Optional[str]:
        """Validate author field (Story #503 Phase 3)."""
        if v is None:
            return None

        # Max length validation
        if len(v) > 255:
            raise ValueError("author field must not exceed 255 characters")

        # Ensure not empty/whitespace
        if not v.strip():
            raise ValueError("author field cannot be empty or contain only whitespace")

        return v.strip()

    @model_validator(mode="after")
    def validate_regex_compatibility(self) -> "SemanticQueryRequest":
        """Validate regex parameter compatibility (Story #503 Phase 1)."""
        if self.regex:
            # regex requires FTS mode
            if self.search_mode not in ["fts", "hybrid"]:
                raise ValueError(
                    "regex=true requires search_mode to be 'fts' or 'hybrid'"
                )
            # regex is incompatible with fuzzy
            if self.fuzzy:
                raise ValueError(
                    "regex=true is incompatible with fuzzy=true (mutual exclusion)"
                )
        return self


class QueryMetadata(BaseModel):
    """Query execution metadata."""

    query_text: str
    execution_time_ms: int
    repositories_searched: int
    timeout_occurred: bool


class SemanticQueryResponse(BaseModel):
    """Response model for semantic query operations."""

    results: List[QueryResultItem]
    total_results: int
    query_metadata: QueryMetadata
    warning: Optional[str] = Field(
        default=None,
        description="Warning message for graceful fallbacks (e.g., missing temporal index)",
    )


class FTSResultItem(BaseModel):
    """Individual FTS (full-text search) result item (Story 5)."""

    path: str = Field(description="File path relative to repository root")
    line_start: int = Field(description="Starting line number of match")
    line_end: int = Field(description="Ending line number of match")
    snippet: str = Field(description="Code snippet with context around match")
    language: str = Field(description="Programming language detected")
    repository_alias: str = Field(description="Repository alias")


class UnifiedSearchMetadata(BaseModel):
    """Unified metadata for all search modes (Story 5)."""

    query_text: str
    search_mode_requested: str = Field(description="Search mode requested by user")
    search_mode_actual: str = Field(description="Actual mode used (after degradation)")
    execution_time_ms: int
    fts_available: bool = Field(description="Whether FTS index is available")
    semantic_available: bool = Field(description="Whether semantic index is available")
    repositories_searched: int = Field(default=0)


class UnifiedSearchResponse(BaseModel):
    """Unified response for all search modes: semantic, FTS, hybrid (Story 5)."""

    search_mode: str = Field(description="Search mode used")
    query: str = Field(description="Query text")
    fts_results: List[FTSResultItem] = Field(
        default_factory=list, description="FTS results (if FTS or hybrid mode)"
    )
    semantic_results: List[QueryResultItem] = Field(
        default_factory=list,
        description="Semantic results (if semantic or hybrid mode)",
    )
    metadata: UnifiedSearchMetadata
