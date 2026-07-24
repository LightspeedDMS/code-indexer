"""Base SCIP indexer interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class IndexerStatus(Enum):
    """Status of SCIP index generation."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class IndexerResult:
    """Result of SCIP index generation."""

    status: IndexerStatus
    duration_seconds: float
    output_file: Optional[Path]
    stdout: str
    stderr: str
    exit_code: int

    def is_success(self) -> bool:
        """Check if indexing succeeded."""
        return self.status == IndexerStatus.SUCCESS

    def is_failure(self) -> bool:
        """Check if indexing failed."""
        return self.status == IndexerStatus.FAILED


class SCIPIndexer(ABC):
    """Abstract base class for SCIP indexers."""

    @abstractmethod
    def generate(
        self,
        project_dir: Path,
        output_dir: Path,
        build_system: str,
        *,
        workspace_id: Optional[str] = None,
        language: Optional[str] = None,
    ) -> IndexerResult:
        """
        Generate SCIP index for a project.

        Args:
            project_dir: Directory containing the project source code
            output_dir: Directory where the .scip file should be generated
            build_system: Build system used by the project (e.g., "maven", "gradle", "npm")
            workspace_id: workspace-mcp id of the golden repo (EVO-65021). Only the
                delegated backend needs it to address the sandbox; local indexers
                ignore it. Keyword-only so existing 3-arg callers are unaffected.
            language: Discovered project language (EVO-65021). Used by the delegated
                backend to select the remote indexer; local indexers ignore it.

        Returns:
            IndexerResult with generation status and details
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """
        Check if the indexer tool is available on the system.

        Returns:
            True if the indexer command can be found, False otherwise
        """
        pass

    @abstractmethod
    def get_version(self) -> Optional[str]:
        """
        Get the version of the indexer tool.

        Returns:
            Version string or None if not available
        """
        pass
