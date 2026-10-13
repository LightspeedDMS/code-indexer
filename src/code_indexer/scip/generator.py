"""SCIP generation orchestration module."""

from .paths import scip_index_dir
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional, Callable, Set
import time
import logging

from .discovery import BuildFileSource, DiscoveredProject, ProjectDiscovery
from .indexers.base import IndexerResult, IndexerStatus
from .indexers.base import UNSUPPORTED_LANGUAGE_EXIT_CODE
from .indexers.base import SCIPIndexer
from .indexers.java import JavaIndexer
from .indexers.typescript import TypeScriptIndexer
from .indexers.python import PythonIndexer
from .indexers.csharp import CSharpIndexer
from .indexers.go import GoIndexer
from .status import ProjectStatus
from .database.schema import DatabaseManager
from .database.builder import SCIPDatabaseBuilder

logger = logging.getLogger(__name__)


# EVO-65021: BSD sysexits ``EX_UNAVAILABLE`` — the exit code the delegated
# sandbox scip wrapper returns for a language it has no indexer for. cidx itself
# also emits an equivalent stderr marker when a discovered language is outside
# its indexer map. Both are EXPECTED, benign outcomes (the Phase-1 sandbox image
# indexes TS/JS only): they must NOT count as a real build failure for the SCIP
# freshness decision, so a mixed-language golden repo can still mark ``fresh``
# instead of thrashing (re-enqueuing a doomed rebuild every refresh cycle).
SCIP_UNSUPPORTED_LANGUAGE_EXIT_CODE = UNSUPPORTED_LANGUAGE_EXIT_CODE
_UNSUPPORTED_LANGUAGE_MARKERS = (
    "no scip indexer for language",
    "no indexer available for language",
)


def indexer_result_is_unsupported_language(indexer_result: IndexerResult) -> bool:
    """True iff a FAILED project failed only because its language is unsupported.

    Distinguishes an EXPECTED, benign skip (exit 69 / "no SCIP indexer for
    language") from a genuine build failure (toolchain/deps/timeout, HTTP 429
    backpressure, or a mandatory database-build failure). A genuine failure
    carries neither signal, so it is NOT treated as unsupported.
    """
    if indexer_result is None:
        return False
    if (
        getattr(indexer_result, "exit_code", None)
        == SCIP_UNSUPPORTED_LANGUAGE_EXIT_CODE
    ):
        return True
    stderr = (getattr(indexer_result, "stderr", "") or "").lower()
    return any(marker in stderr for marker in _UNSUPPORTED_LANGUAGE_MARKERS)


@dataclass
class ProjectGenerationResult:
    """Result of generating SCIP index for a single project."""

    project: DiscoveredProject
    indexer_result: IndexerResult


@dataclass
class GenerationResult:
    """Overall result of SCIP generation for all projects."""

    total_projects: int
    successful_projects: int
    failed_projects: int
    project_results: List[ProjectGenerationResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    # EVO-65021: of ``failed_projects``, how many failed only because their
    # language is unsupported by the (Phase-1) SCIP backend. These are EXPECTED,
    # benign skips — subtracting them yields the count of *real* build failures.
    unsupported_language_projects: int = 0

    def is_complete_success(self) -> bool:
        """Check if all projects succeeded."""
        return self.total_projects > 0 and self.failed_projects == 0

    def is_partial_success(self) -> bool:
        """Check if some (but not all) projects succeeded."""
        return self.successful_projects > 0 and self.failed_projects > 0

    def is_complete_failure(self) -> bool:
        """Check if all projects failed."""
        return self.total_projects > 0 and self.successful_projects == 0

    def real_failed_projects(self) -> int:
        """Count of failures that are NOT expected unsupported-language skips."""
        return max(0, self.failed_projects - self.unsupported_language_projects)

    def is_fresh_eligible(self) -> bool:
        """EVO-65021: safe to record the ``.build_commit`` freshness marker.

        True iff NONE of the discovered projects failed for an UNEXPECTED
        reason. An unsupported-language exit (69) is expected and benign, so a
        repo whose only failures are unsupported languages (a mixed TS +
        Java/Python golden repo, or a pure non-TS repo) is still fresh-eligible
        and will not thrash. A repo with a genuine build failure is NOT
        fresh-eligible, so it is retried and does not poison the marker (the
        MAJOR-3 partial-masking guarantee, kept intact for real failures).

        Zero-project case (EVO-65021 fix): a repo where ``ProjectDiscovery``
        found nothing to index is legitimately CONVERGED — there is nothing to
        build. It is therefore fresh-eligible so ``on_success`` fires and the
        ``.build_commit`` marker is written, rather than the repo re-enqueueing
        an identical empty rebuild every refresh cycle forever. This is gated on
        an EMPTY ``project_results`` (nothing was attempted): a genuine
        no-discovery run produces no per-project results, whereas a FAILED build
        always carries them — so a real failure can never be mistaken for
        convergence and marked fresh.
        """
        if self.total_projects == 0 and not self.project_results:
            return self.failed_projects == 0
        return self.total_projects > 0 and self.real_failed_projects() == 0


class SCIPGenerator:
    """Orchestrates SCIP index generation for multiple projects."""

    def __init__(
        self,
        repo_root: Path,
        max_workers: int = 4,
        *,
        indexer_map: Optional[Dict[str, SCIPIndexer]] = None,
        workspace_id: Optional[str] = None,
        golden_alias: Optional[str] = None,
        allow_degenerate: bool = False,
        build_file_source: Optional[BuildFileSource] = None,
    ):
        """
        Initialize SCIP generator.

        Args:
            repo_root: Root directory of repository
            max_workers: Maximum number of parallel indexer executions
            indexer_map: Optional ``language -> SCIPIndexer`` map to inject
                (EVO-65021 backend seam). Defaults to today's local subprocess
                indexers (built inline below), so the default path is
                byte-for-byte the previous behaviour. Prefer
                ``build_scip_generator`` (startup wiring) to select the backend
                from config rather than constructing a map here.
            workspace_id: workspace-mcp id of the golden repo (EVO-65021),
                threaded into every ``indexer.generate`` call. Only the
                delegated backend uses it; ``None`` (default) is correct for the
                local backend.
            golden_alias: Golden-repo alias (EVO-65365 Option C), threaded into
                every ``indexer.generate`` call. The delegated backend addresses
                the sandbox build by this alias when the golden repo has no
                workspace_id (the common case — cidx-owned golden repos have
                none). Only the delegated backend uses it; ``None`` (default) is
                correct for the local backend.
            allow_degenerate: EVO-65365. Threaded into every ``indexer.generate``
                call. When True (set for golden repos — best-effort builds), the
                delegated backend opts into the sandbox's locals-only fallback so a
                bare/unbuilt golden repo emits a degenerate index rather than
                exit-71. Only the delegated backend uses it; ``False`` (default) is
                correct for the local backend and the non-golden delegated path.
            build_file_source: Where project discovery asks about the
                repository's files. ``None`` (default) = this machine's disk; a
                pod without the repository passes a content-seam source
                (``server/services/scip_repo_reads.build_file_source``).
        """
        self.repo_root = Path(repo_root)
        self._build_file_source = build_file_source
        self.scip_dir = scip_index_dir(self.repo_root)
        self.max_workers = max_workers
        self.workspace_id = workspace_id
        self.golden_alias = golden_alias
        self.allow_degenerate = allow_degenerate

        # Initialize indexers. Default = today's local subprocess map (built
        # inline so tests that patch these classes at this module keep working);
        # injectable for the delegated backend via ``indexer_map`` (EVO-65021).
        # NOTE: keep this default map in sync with
        # ``ScipBackendFactory.create_local_indexer_map`` — the two cannot share
        # one builder without a generator<->factory import cycle.
        self._indexers: Dict[str, SCIPIndexer] = (
            indexer_map
            if indexer_map is not None
            else {
                "java": JavaIndexer(),
                "kotlin": JavaIndexer(),  # Kotlin uses scip-java
                "typescript": TypeScriptIndexer(),
                "python": PythonIndexer(),
                "csharp": CSharpIndexer(),
                "go": GoIndexer(),
            }
        )

    def _cleanup_orphan_scip_databases(
        self, discovered_projects: List[DiscoveredProject]
    ) -> int:
        """Remove SCIP index databases for projects no longer in the codebase.

        Scans .code-indexer/scip/ for index.scip.db files and removes any that
        do not correspond to a currently-discovered project. This prevents stale
        SCIP data from accumulating when branches are switched or projects removed.

        Args:
            discovered_projects: List of projects currently discovered in the repo.
                                 Only databases matching these projects are kept.

        Returns:
            Number of orphan databases removed.

        Note:
            When scip_dir does not exist, returns 0 immediately.
            Empty parent directories are removed after database deletion (best-effort).
        """
        if not self.scip_dir.exists():
            return 0

        # Build set of relative paths for discovered projects
        # Database paths are: scip_dir / project.relative_path / "index.scip.db"
        live_project_paths: Set[Path] = {
            self.scip_dir / p.relative_path for p in discovered_projects
        }

        # Scan for all index.scip.db files
        orphan_count = 0
        for db_file in list(self.scip_dir.rglob("index.scip.db")):
            # The project directory is db_file.parent
            project_dir = db_file.parent
            if project_dir not in live_project_paths:
                # This is an orphan - delete it
                try:
                    db_file.unlink()
                    orphan_count += 1
                    logger.info(f"Removed orphan SCIP database: {db_file}")

                    # Clean up empty parent directories (best-effort)
                    try:
                        # Walk up removing empty dirs, but not scip_dir itself
                        parent = project_dir
                        while (
                            parent != self.scip_dir and parent != self.scip_dir.parent
                        ):
                            if parent.exists() and not any(parent.iterdir()):
                                parent.rmdir()
                                logger.debug(f"Removed empty SCIP directory: {parent}")
                            else:
                                break
                            parent = parent.parent
                    except Exception as e:
                        logger.debug(
                            f"Could not clean up directory after orphan removal: {e}"
                        )

                except Exception as e:
                    logger.warning(
                        f"Failed to remove orphan SCIP database {db_file}: {e}"
                    )

        if orphan_count > 0:
            logger.info(f"SCIP orphan cleanup: removed {orphan_count} orphan databases")

        return orphan_count

    def generate(
        self,
        progress_callback: Optional[Callable[[DiscoveredProject, str], None]] = None,
    ) -> GenerationResult:
        """
        Generate SCIP indexes for all discoverable projects.

        Thin span-wrapping public entry point (Story #1586 AC5) -- the full
        implementation lives in _generate_impl(), unchanged, so wrapping it
        in a custom span never requires re-indenting that large body.

        Story #1586 code-review Finding 6: create_span() is imported HERE,
        function-local, rather than at module scope -- this module is on
        the CLI's `cidx scip generate` import path, and a module-scope
        import of the server telemetry package transitively pulls in ~11
        server-layer modules into every CLI invocation (measured ~+0.1s),
        violating this project's CLI/server layering boundary (Bug #1468
        precedent). create_span() no-ops (yields a _NoOpSpan) when OTEL
        tracing is unavailable/uninitialized (always true on the CLI-only
        path).
        """
        from code_indexer.server.telemetry.spans import create_span

        with create_span(
            "cidx.scip.generate",
            attributes={"repo_root": str(self.repo_root)},
        ):
            return self._generate_impl(progress_callback)

    def _generate_impl(
        self,
        progress_callback: Optional[Callable[[DiscoveredProject, str], None]] = None,
    ) -> GenerationResult:
        """
        Generate SCIP indexes for all discoverable projects.

        Args:
            progress_callback: Optional callback for progress reporting
                Called with (project, status_message)

        Returns:
            GenerationResult with summary of generation
        """
        start_time = time.time()

        # Discover projects
        discovery = ProjectDiscovery(self.repo_root, self._build_file_source)
        projects = discovery.discover()

        # Clean up orphan SCIP databases before generation
        # This ensures stale data from deleted/moved projects is removed
        self._cleanup_orphan_scip_databases(projects)

        if not projects:
            return GenerationResult(
                total_projects=0,
                successful_projects=0,
                failed_projects=0,
                duration_seconds=time.time() - start_time,
            )

        # Generate indexes (sequential for now, parallel coming next)
        project_results = []
        successful = 0
        failed = 0
        unsupported = 0  # EVO-65021: benign unsupported-language skips (exit 69)

        for project in projects:
            if progress_callback:
                progress_callback(
                    project,
                    f"Generating SCIP for {project.relative_path} [{project.language}]...",
                )

            # Get appropriate indexer
            indexer = self._indexers.get(project.language)
            if not indexer:
                # Create failed result for unsupported language
                result = IndexerResult(
                    status=IndexerStatus.FAILED,
                    duration_seconds=0.0,
                    output_file=None,
                    stdout="",
                    stderr=f"No indexer available for language: {project.language}",
                    exit_code=-1,
                )
                project_results.append(ProjectGenerationResult(project, result))
                failed += 1
                unsupported += 1  # cidx has no indexer for this language (benign)
                continue

            # Generate index
            project_dir = self.repo_root / project.relative_path
            output_dir = self.scip_dir / project.relative_path

            indexer_result = indexer.generate(
                project_dir,
                output_dir,
                project.build_system,
                workspace_id=self.workspace_id,
                golden_alias=self.golden_alias,
                language=project.language,
                allow_degenerate=self.allow_degenerate,
            )

            project_results.append(ProjectGenerationResult(project, indexer_result))

            if indexer_result.is_success():
                successful += 1
                if progress_callback:
                    progress_callback(
                        project,
                        f"✓ {project.relative_path} ({indexer_result.duration_seconds:.1f}s)",
                    )

                # Build database from protobuf - REQUIRED FOR SUCCESS
                if indexer_result.output_file:
                    try:
                        scip_file = indexer_result.output_file
                        db_manager = DatabaseManager(scip_file)
                        db_manager.create_schema()

                        builder = SCIPDatabaseBuilder()
                        builder.build(scip_file, db_manager.db_path)

                        db_manager.create_indexes()
                    except Exception as e:
                        # Database build is MANDATORY - failure means generation failed
                        # Per Anti-Fallback Foundation #2: graceful failure over forceful success
                        logger.error(
                            f"Database build failed for {project.relative_path}: {e}",
                            exc_info=True,
                        )
                        # Convert success to failure
                        successful -= 1
                        failed += 1
                        # Update result status
                        indexer_result.status = IndexerStatus.FAILED
                        indexer_result.stderr = f"Database build failed: {e}"
            else:
                failed += 1
                # EVO-65021: a delegated sandbox exits 69 for a language its
                # (Phase-1) image cannot build — an expected, benign skip that
                # must not block freshness. A genuine failure is not counted.
                if indexer_result_is_unsupported_language(indexer_result):
                    unsupported += 1
                if progress_callback:
                    progress_callback(project, f"✗ {project.relative_path} (failed)")

        duration = time.time() - start_time

        return GenerationResult(
            total_projects=len(projects),
            successful_projects=successful,
            failed_projects=failed,
            project_results=project_results,
            duration_seconds=duration,
            unsupported_language_projects=unsupported,
        )

    def rebuild_projects(
        self,
        project_paths: List[str],
        force: bool = False,
        failed_only: bool = False,
        progress_callback: Optional[Callable[[DiscoveredProject, str], None]] = None,
    ) -> Dict[str, ProjectStatus]:
        """
        Rebuild SCIP indexes for specific projects.

        Args:
            project_paths: List of project paths to rebuild
            force: Rebuild even if project already succeeded
            failed_only: Rebuild all failed projects (ignores project_paths)
            progress_callback: Optional callback for progress reporting

        Returns:
            Dict mapping project path to updated ProjectStatus
        """
        from .status import StatusTracker, ProjectStatus, OverallStatus
        from datetime import datetime

        # Load current status
        tracker = StatusTracker(self.scip_dir)
        current_status = tracker.load()

        # Clean up orphan SCIP databases before rebuilding
        # This ensures stale data from deleted/moved projects is removed
        discovered_projects = ProjectDiscovery(
            self.repo_root, self._build_file_source
        ).discover()
        self._cleanup_orphan_scip_databases(discovered_projects)

        # Determine which projects to rebuild
        if failed_only:
            projects_to_rebuild = [
                path
                for path, status in current_status.projects.items()
                if status.status == OverallStatus.FAILED
            ]
        else:
            projects_to_rebuild = project_paths

        # Validate projects exist in status
        results = {}
        for project_path in projects_to_rebuild:
            if project_path not in current_status.projects:
                raise ValueError(f"Unknown project path: {project_path}")

            project_status = current_status.projects[project_path]

            # Skip if already successful and not forcing
            if not force and project_status.status == OverallStatus.SUCCESS:
                continue

            # Get indexer for this project
            indexer = self._indexers.get(project_status.language)
            if not indexer:
                # No indexer available
                results[project_path] = ProjectStatus(
                    status=OverallStatus.FAILED,
                    language=project_status.language,
                    build_system=project_status.build_system,
                    timestamp=datetime.now().isoformat(),
                    error_message=f"No indexer available for language: {project_status.language}",
                    exit_code=-1,
                )
                continue

            # Progress reporting
            if progress_callback:
                project = DiscoveredProject(
                    relative_path=Path(project_path),
                    language=project_status.language,
                    build_system=project_status.build_system,
                    build_file=Path(project_path),  # Simplified for rebuild
                )
                progress_callback(
                    project,
                    f"Rebuilding SCIP for {project_path} [{project_status.language}]...",
                )

            # Rebuild
            project_dir = self.repo_root / project_path
            output_dir = self.scip_dir / project_path

            indexer_result = indexer.generate(
                project_dir,
                output_dir,
                project_status.build_system,
                workspace_id=self.workspace_id,
                golden_alias=self.golden_alias,
                language=project_status.language,
                allow_degenerate=self.allow_degenerate,
            )

            # Build database from protobuf if successful - REQUIRED FOR SUCCESS
            if indexer_result.is_success() and indexer_result.output_file:
                try:
                    scip_file = indexer_result.output_file
                    db_manager = DatabaseManager(scip_file)
                    db_manager.create_schema()

                    builder = SCIPDatabaseBuilder()
                    builder.build(scip_file, db_manager.db_path)

                    db_manager.create_indexes()
                except Exception as e:
                    # Database build is MANDATORY - failure means rebuild failed
                    # Per Anti-Fallback Foundation #2: graceful failure over forceful success
                    logger.error(
                        f"Database build failed for {project_path}: {e}",
                        exc_info=True,
                    )
                    # Update result status to reflect database failure
                    indexer_result.status = IndexerStatus.FAILED
                    indexer_result.stderr = f"Database build failed: {e}"

            # Create updated status
            new_status = ProjectStatus(
                status=(
                    OverallStatus.SUCCESS
                    if indexer_result.is_success()
                    else OverallStatus.FAILED
                ),
                language=project_status.language,
                build_system=project_status.build_system,
                timestamp=datetime.now().isoformat(),
                duration_seconds=indexer_result.duration_seconds,
                output_file=(
                    str(indexer_result.output_file)
                    if indexer_result.output_file
                    else None
                ),
                error_message=indexer_result.stderr if indexer_result.stderr else None,
                exit_code=indexer_result.exit_code,
                stdout=indexer_result.stdout,
                stderr=indexer_result.stderr,
            )

            results[project_path] = new_status

            # Update status in tracker
            current_status.projects[project_path] = new_status

        # Recalculate overall status
        successful = sum(
            1
            for s in current_status.projects.values()
            if s.status == OverallStatus.SUCCESS
        )
        failed = sum(
            1
            for s in current_status.projects.values()
            if s.status == OverallStatus.FAILED
        )

        if failed == 0:
            current_status.overall_status = OverallStatus.SUCCESS
        elif successful > 0 and failed > 0:
            current_status.overall_status = OverallStatus.LIMBO
        elif successful == 0:
            current_status.overall_status = OverallStatus.FAILED

        current_status.successful_projects = successful
        current_status.failed_projects = failed

        # Save updated status
        tracker.save(current_status)

        return results
