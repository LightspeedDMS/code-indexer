"""
Job and indexing Pydantic models for CIDX Server API.

Extracted from app.py as part of Story #409 (app.py modularization).
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, model_validator


class AddIndexRequest(BaseModel):
    """Request model for adding index to golden repository.

    Supports two modes:
    - Single index: index_type (string) - for backward compatibility
    - Multi-select: index_types (array) - for UI multi-select

    At least one of index_type or index_types must be provided.
    If both are provided, index_types takes precedence.
    """

    index_type: Optional[str] = Field(
        None, description="Single index type: semantic, fts, temporal, or scip"
    )
    index_types: Optional[List[str]] = Field(
        None,
        description="Array of index types for multi-select: semantic, fts, temporal, scip",
    )

    @model_validator(mode="after")
    def validate_at_least_one_type(self) -> "AddIndexRequest":
        """Ensure at least one of index_type or index_types is provided."""
        if not self.index_type and not self.index_types:
            raise ValueError("Either index_type or index_types must be provided")
        if self.index_types is not None and len(self.index_types) == 0:
            raise ValueError("index_types cannot be empty")
        return self

    def get_index_types(self) -> List[str]:
        """Get the list of index types to process.

        Returns index_types if provided, otherwise wraps index_type in a list.
        """
        if self.index_types:
            return self.index_types
        return [self.index_type] if self.index_type else []


class AddIndexResponse(BaseModel):
    """Response model for add index operation.

    Supports two modes:
    - Single job: job_id (string), status - for backward compatibility
    - Multi-job: job_ids (array), statuses - for multi-select
    """

    job_id: Optional[str] = None  # Single job ID (backward compatible)
    job_ids: Optional[List[str]] = None  # Multiple job IDs (multi-select)
    status: str = "pending"


class IndexInfo(BaseModel):
    """Model for index presence information."""

    present: bool
    last_updated: Optional[str] = None
    size_bytes: Optional[int] = None


class IndexStatusResponse(BaseModel):
    """Response model for index status query."""

    alias: str
    indexes: Dict[str, IndexInfo]


class JobResponse(BaseModel):
    """Response model for background job operations."""

    job_id: str
    message: str


class JobStatusResponse(BaseModel):
    """Response model for job status queries."""

    job_id: str
    operation_type: str
    status: str
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    progress: int
    result: Optional[Dict[str, Any]]
    error: Optional[str]
    username: str  # Added for user tracking
    # Story #480: Real-time phase progress fields (AC7)
    current_phase: Optional[str] = None  # e.g., "semantic", "temporal", "cow"
    phase_detail: Optional[str] = None  # e.g., "150/500 files indexed"


class JobListResponse(BaseModel):
    """Response model for job listing."""

    jobs: List[JobStatusResponse]
    total: int
    limit: int
    offset: int


class JobCancellationResponse(BaseModel):
    """Response model for job cancellation."""

    success: bool
    message: str


class JobCleanupResponse(BaseModel):
    """Response model for job cleanup."""

    cleaned_count: int
    message: str


class SyncProgress(BaseModel):
    """Model for sync progress information."""

    percentage: int = Field(ge=0, le=100, description="Progress percentage")
    files_processed: int = Field(ge=0, description="Number of files processed")
    files_total: int = Field(ge=0, description="Total number of files to process")
    current_file: Optional[str] = Field(description="Currently processing file")


class SyncJobOptions(BaseModel):
    """Model for sync job options."""

    force: bool
    full_reindex: bool
    incremental: bool
