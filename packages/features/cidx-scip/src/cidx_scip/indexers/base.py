"""Base SCIP indexer interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

#: BSD sysexits ``EX_UNAVAILABLE``: the exit code a delegated build reports for a
#: language it has no indexer for. ``generator.indexer_result_is_unsupported_language``
#: reads it as a benign skip rather than a genuine build failure.
UNSUPPORTED_LANGUAGE_EXIT_CODE = 69

#: Put in a FAILED result's stderr when a delegated backend was temporarily full
#: (no sandbox capacity). The delegated build queue re-enqueues such a build
#: exactly as it does an HTTP 429 (``scip_resolution_queue._BACKPRESSURE_MARKERS``).
DELEGATED_BACKPRESSURE_MARKER = "sandbox backpressure"


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
    # EVO-65365: True when the delegated build emitted a DEGENERATE (locals-only)
    # index instead of a full one — the sandbox's ``allow_degenerate`` path for a
    # bare/unbuilt golden repo. Defaults False (full index / local indexers).
    # Surfaced here so a degenerate index is OBSERVABLE and distinct from a full
    # one; the delegated-build worker records the ``delegated_degenerate`` metric
    # from it (the pure indexer layer holds no server-metrics dependency).
    degenerate: bool = False

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
        golden_alias: Optional[str] = None,
        language: Optional[str] = None,
        allow_degenerate: bool = False,
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
            golden_alias: Golden-repo alias (EVO-65365 Option C). The delegated
                backend addresses the sandbox build by this alias when no
                workspace_id exists (cidx-owned golden repos have none); exactly
                one of workspace_id/golden_alias is set. Local indexers ignore it.
            language: Discovered project language (EVO-65021). Used by the delegated
                backend to select the remote indexer; local indexers ignore it.
            allow_degenerate: EVO-65365. When True, the delegated backend opts the
                build into the sandbox's ``allow_degenerate`` path — a bare/unbuilt
                (golden) repo emits a locals-only index instead of exit-71. Only the
                delegated backend forwards it; local indexers ignore it. Keyword-only.

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
