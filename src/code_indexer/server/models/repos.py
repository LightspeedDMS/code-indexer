"""
Repository Pydantic models for CIDX Server API.

Extracted from app.py as part of Story #409 (app.py modularization).
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

from .api_models import TemporalIndexOptions


class AddGoldenRepoRequest(BaseModel):
    """Request model for adding golden repositories."""

    repo_url: str = Field(
        ..., min_length=1, max_length=1000, description="Git repository URL"
    )
    alias: str = Field(
        ..., min_length=1, max_length=100, description="Unique alias for repository"
    )
    default_branch: str = Field(
        default="main", min_length=1, max_length=100, description="Default branch"
    )
    description: Optional[str] = Field(
        default=None, max_length=500, description="Optional repository description"
    )
    enable_temporal: bool = False
    temporal_options: Optional[TemporalIndexOptions] = None

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, v: str) -> str:
        """Validate repository URL format."""
        v = v.strip()
        if not v:
            raise ValueError("Repository URL cannot be empty")
        if not v.startswith(("http://", "https://", "git@", "file://", "/")):
            raise ValueError("Repository URL must be a valid HTTP(S), SSH, or file URL")
        return v

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, v: str) -> str:
        """Validate alias format."""
        v = v.strip()
        if not v:
            raise ValueError("Alias cannot be empty")
        # Allow alphanumeric, hyphens, and underscores
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                "Alias must contain only alphanumeric characters, hyphens, and underscores"
            )
        return v

    @field_validator("default_branch")
    @classmethod
    def validate_default_branch(cls, v: str) -> str:
        """Validate branch name."""
        v = v.strip()
        if not v:
            raise ValueError("Branch name cannot be empty")
        return v


class GoldenRepoInfo(BaseModel):
    """Model for golden repository information."""

    alias: str
    repo_url: str
    default_branch: str
    clone_path: str
    created_at: str


class ActivateRepositoryRequest(BaseModel):
    """Request model for activating repositories."""

    golden_repo_alias: Optional[str] = Field(
        None,
        min_length=1,
        max_length=255,
        description="Golden repository alias to activate (single repo)",
    )
    golden_repo_aliases: Optional[List[str]] = Field(
        None,
        description="Golden repository aliases for composite activation (multi-repo)",
    )
    branch_name: Optional[str] = Field(
        None,
        max_length=255,
        description="Branch to activate (defaults to golden repo's default branch)",
    )
    user_alias: Optional[str] = Field(
        None,
        min_length=1,
        max_length=255,
        description="User's alias for the repo (defaults to golden_repo_alias)",
    )

    @model_validator(mode="after")
    def validate_repo_parameters(self) -> "ActivateRepositoryRequest":
        """Validate mutual exclusivity and requirements for repository parameters."""
        golden_alias = self.golden_repo_alias
        golden_aliases = self.golden_repo_aliases

        # Check mutual exclusivity
        if golden_alias and golden_aliases:
            raise ValueError(
                "Cannot specify both golden_repo_alias and golden_repo_aliases"
            )

        # Validate composite repository requirements BEFORE checking if at least one is provided
        # This ensures empty lists get the correct error message
        if golden_aliases is not None:
            if len(golden_aliases) < 2:
                raise ValueError(
                    "Composite activation requires at least 2 repositories"
                )

            # Validate each alias in the list
            for alias in golden_aliases:
                if not alias or not alias.strip():
                    raise ValueError(
                        "Golden repo aliases cannot contain empty or whitespace-only strings"
                    )

        # Check that at least one is provided (after composite validation)
        if not golden_alias and not golden_aliases:
            raise ValueError(
                "Must specify either golden_repo_alias or golden_repo_aliases"
            )

        return self

    @field_validator("golden_repo_alias")
    @classmethod
    def validate_golden_repo_alias(cls, v: Optional[str]) -> Optional[str]:
        """Validate golden repo alias is not empty or whitespace-only."""
        if v is not None and (not v or not v.strip()):
            raise ValueError(
                "Golden repo alias cannot be empty or contain only whitespace"
            )
        return v.strip() if v else None

    @field_validator("user_alias")
    @classmethod
    def validate_user_alias(cls, v: Optional[str]) -> Optional[str]:
        """Validate user alias if provided."""
        if v is not None and (not v or not v.strip()):
            raise ValueError("User alias cannot be empty or contain only whitespace")
        return v.strip() if v else None


class ActivatedRepositoryInfo(BaseModel):
    """
    Model for activated repository information.

    Supports both single and composite repositories:
    - Single repos: Have golden_repo_alias and current_branch
    - Composite repos: Have golden_repo_aliases and discovered_repos instead
    """

    user_alias: str
    golden_repo_alias: Optional[str] = None  # Optional for composite repos
    current_branch: Optional[str] = None  # Optional for composite repos
    activated_at: str
    last_accessed: str


class SwitchBranchRequest(BaseModel):
    """Request model for switching repository branch."""

    branch_name: str = Field(
        ..., min_length=1, max_length=255, description="Branch name to switch to"
    )
    create: bool = Field(default=False, description="Create branch if it doesn't exist")

    @field_validator("branch_name")
    @classmethod
    def validate_branch_name(cls, v: str) -> str:
        """Validate branch name is not empty or whitespace-only."""
        if not v or not v.strip():
            raise ValueError("Branch name cannot be empty or contain only whitespace")
        return v.strip()


class RepositoryInfo(BaseModel):
    """Model for basic repository information."""

    alias: str
    repo_url: str
    default_branch: str
    created_at: str


class RepositoryDetailsResponse(BaseModel):
    """Model for detailed repository information."""

    alias: str
    repo_url: str
    default_branch: str
    clone_path: str
    created_at: str
    activation_status: str
    branches_list: List[str]
    file_count: int
    index_size: int
    last_updated: str
    enable_temporal: bool = False
    temporal_status: Optional[Dict[str, Any]] = None


class RepositoryListResponse(BaseModel):
    """Response model for repository listing endpoints."""

    repositories: List[ActivatedRepositoryInfo]
    total: int


class AvailableRepositoryListResponse(BaseModel):
    """Response model for available repository listing endpoint."""

    repositories: List[RepositoryInfo]
    total: int


class RepositorySyncResponse(BaseModel):
    """Response model for repository sync operation."""

    message: str
    changes_applied: bool
    files_changed: Optional[int] = None
    changed_files: Optional[List[str]] = None


class BranchInfo(BaseModel):
    """Model for individual branch information."""

    name: str
    type: str  # "local" or "remote"
    is_current: bool
    remote_ref: Optional[str] = None
    last_commit_hash: Optional[str] = None
    last_commit_message: Optional[str] = None
    last_commit_date: Optional[str] = None


class RepositoryBranchesResponse(BaseModel):
    """Response model for repository branches listing."""

    branches: List[BranchInfo]
    current_branch: str
    total_branches: int
    local_branches: int
    remote_branches: int


class RepositoryStatistics(BaseModel):
    """Model for repository statistics."""

    total_files: int
    indexed_files: int
    total_size_bytes: int
    embeddings_count: int
    languages: List[str]


class GitInfo(BaseModel):
    """Model for git repository information."""

    current_branch: str
    branches: List[str]
    last_commit: str
    remote_url: Optional[str] = None


class RepositoryConfiguration(BaseModel):
    """Model for repository configuration."""

    ignore_patterns: List[str]
    chunk_size: int
    overlap: int
    embedding_model: str


class RepositoryDetailsV2Response(BaseModel):
    """Model for detailed repository information (API v2 response)."""

    id: str
    name: str
    path: str
    owner_id: str
    created_at: str
    updated_at: str
    last_sync_at: Optional[str] = None
    status: str  # "indexed", "indexing", "error", "pending"
    indexing_progress: float  # 0-100
    statistics: RepositoryStatistics
    git_info: GitInfo
    configuration: RepositoryConfiguration
    errors: List[str]


class ComponentRepoInfo(BaseModel):
    """Information about each component repository in a composite repository."""

    name: str
    path: str
    has_index: bool
    collection_exists: bool
    indexed_files: int
    last_indexed: Optional[datetime] = None
    size_mb: float


class CompositeRepositoryDetails(BaseModel):
    """Details for a composite repository with aggregated component information."""

    user_alias: str
    is_composite: bool = True
    activated_at: datetime
    last_accessed: datetime
    component_repositories: List[ComponentRepoInfo]
    total_files: int
    total_size_mb: float

    @field_serializer("activated_at", "last_accessed")
    def serialize_datetime(self, value: datetime) -> str:
        """Serialize datetime to ISO format."""
        return value.isoformat()


class RepositorySyncRequest(BaseModel):
    """Request model for repository synchronization."""

    force: bool = Field(
        default=False, description="Force sync by cancelling existing sync jobs"
    )
    full_reindex: bool = Field(
        default=False, description="Perform full reindexing instead of incremental"
    )
    incremental: bool = Field(
        default=True, description="Perform incremental sync for changed files only"
    )
    pull_remote: bool = Field(
        default=False, description="Pull from remote repository before sync"
    )
    remote: str = Field(
        default="origin", description="Remote name for git pull operation"
    )
    ignore_patterns: Optional[List[str]] = Field(
        default=None, description="Additional ignore patterns for this sync"
    )
    progress_webhook: Optional[str] = Field(
        default=None, description="Webhook URL for progress updates"
    )


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


class RepositorySyncJobResponse(BaseModel):
    """Response model for repository sync job submission."""

    job_id: str = Field(description="Unique job identifier")
    status: str = Field(description="Job status (queued, running, completed, failed)")
    repository_id: str = Field(description="Repository identifier being synced")
    created_at: str = Field(description="Job creation timestamp")
    estimated_completion: Optional[str] = Field(description="Estimated completion time")
    progress: SyncProgress = Field(description="Current sync progress")
    options: SyncJobOptions = Field(description="Sync job options")


class GeneralRepositorySyncRequest(BaseModel):
    """Request model for general repository synchronization via repository alias."""

    repository_alias: str = Field(description="Repository alias to synchronize")
    force: bool = Field(
        default=False, description="Force sync by cancelling existing sync jobs"
    )
    full_reindex: bool = Field(
        default=False, description="Perform full reindexing instead of incremental"
    )
    incremental: bool = Field(
        default=True, description="Perform incremental sync for changed files only"
    )
    pull_remote: bool = Field(
        default=False, description="Pull from remote repository before sync"
    )
    remote: str = Field(
        default="origin", description="Remote name for git pull operation"
    )
    ignore_patterns: Optional[List[str]] = Field(
        default=None, description="Additional ignore patterns for this sync"
    )
    progress_webhook: Optional[str] = Field(
        default=None, description="Webhook URL for progress updates"
    )
