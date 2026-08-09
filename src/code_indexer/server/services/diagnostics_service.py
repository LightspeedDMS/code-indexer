"""
Diagnostics Service for CIDX Server.

Provides diagnostic checks across five categories:
- CLI Tool Dependencies (ripgrep, Git, Coursier, Claude CLI, SCIP tools)
- SDK Prerequisites (.NET, Go, Node.js)
- External API Integrations (GitHub, GitLab, OIDC, OpenTelemetry)
- Credential & Connectivity (SSH keys, GitHub/GitLab tokens)
- Core Infrastructure (SQLite database, vector storage)

Features caching with category-specific TTLs and persistence to SQLite database.
"""

import asyncio
import itertools
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Any, Optional, Tuple

import httpx

from code_indexer.server.services.ci_token_manager import (
    CITokenManager,
    GITHUB_TOKEN_PATTERN,
    GITLAB_TOKEN_PATTERN,
)
from code_indexer.server.storage.database_manager import DatabaseConnectionManager
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

if TYPE_CHECKING:
    from code_indexer.server.storage.protocols import DiagnosticsBackend

# Timeout for individual diagnostic checks (seconds)
DIAGNOSTIC_TIMEOUT_SECONDS = 10.0

# Timeout for external API checks (seconds) - AC6
API_TIMEOUT_SECONDS = 30.0

# Timeout for SSH connectivity checks (seconds) - Story S5 AC6
SSH_TIMEOUT_SECONDS = 60.0

# Cache TTL by category (per epic spec)
DEFAULT_CACHE_TTL = timedelta(minutes=10)  # General diagnostics
API_CACHE_TTL = timedelta(minutes=5)  # External API validations (AC8)

# HNSW index validation constants (Bug #147)
DEFAULT_HNSW_MAX_ELEMENTS = (
    1000000  # Must match ServerResourceConfig.hnsw_max_elements default
)

# CLI Tool configurations
CLI_TOOLS = [
    {"name": "ripgrep", "command": "rg --version", "required_sdk": None},
    {"name": "Git", "command": "git --version", "required_sdk": None},
    {"name": "Coursier", "command": "cs --version", "required_sdk": None},
    {"name": "Claude CLI", "command": "claude --version", "required_sdk": None},
    {"name": "scip-python", "command": "scip-python --version", "required_sdk": None},
    {
        "name": "scip-typescript",
        "command": "scip-typescript --version",
        "required_sdk": "nodejs",
    },
    {
        "name": "scip-dotnet",
        "command": "scip-dotnet --version",
        "required_sdk": "dotnet",
    },
    {"name": "scip-go", "command": "scip-go --version", "required_sdk": "go"},
]

# SDK Prerequisites configurations
SDK_PREREQUISITES = [
    {"name": ".NET SDK", "command": "dotnet --version", "key": "dotnet"},
    {"name": "Go SDK", "command": "go version", "key": "go"},
    {"name": "Node.js/npm", "command": "npm --version", "key": "nodejs"},
]


class DiagnosticStatus(Enum):
    """Status values for diagnostic checks."""

    WORKING = "working"
    WARNING = "warning"
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"
    NOT_APPLICABLE = "not_applicable"
    RUNNING = "running"
    NOT_RUN = "not_run"


class DiagnosticCategory(Enum):
    """Categories for diagnostic grouping."""

    CLI_TOOLS = "cli_tools"
    SDK_PREREQUISITES = "sdk_prerequisites"
    EXTERNAL_APIS = "external_apis"
    CREDENTIALS = "credentials"
    INFRASTRUCTURE = "infrastructure"


@dataclass
class DiagnosticResult:
    """Result of a single diagnostic check."""

    name: str
    status: DiagnosticStatus
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert result to dictionary for serialization."""
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp.isoformat(),
        }


class DiagnosticsService:
    """
    Service for running diagnostic checks across five categories.

    Fully implements all diagnostic checks with real system validation.

    Features:
    - Category-specific caching (10-min default, 5-min for APIs, 1-hour for feedback)
    - Async parallel execution of diagnostic checks
    - Per-category execution support with cache validation
    - Running state tracking for all categories and individual categories
    - SQLite persistence for diagnostic results
    - Exception isolation ensures all categories run independently
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        storage_backend: "Optional[DiagnosticsBackend]" = None,
    ):
        """
        Initialize diagnostics service with empty cache.

        Args:
            db_path: Path to SQLite database file. If None, uses default location
                     from CIDX_SERVER_DATA_DIR or ~/.cidx-server/data/cidx_server.db
            storage_backend: Optional DiagnosticsBackend for DB delegation
        """
        self._cache: Dict[DiagnosticCategory, List[DiagnosticResult]] = {}
        self._cache_timestamps: Dict[DiagnosticCategory, datetime] = {}
        self._cache_ttl = timedelta(minutes=10)
        self._running = False
        self._running_categories: set = set()
        # Story #1491 AC4: a threading.Lock, NOT an asyncio.Lock.
        #
        # The precise reason: both diagnostics entry points are now dispatched
        # as SYNC Starlette background tasks, and each runs its coroutine on
        # its OWN private event loop via asyncio.run() inside a Starlette
        # threadpool worker. Two such runs (run-all and run-category, or two
        # categories) can therefore be in flight on two different threads with
        # two different loops. An asyncio.Lock offers ZERO protection across
        # threads, and being loop-bound it raises outright once contended from
        # a second loop -- so it is not merely weaker here, it is incorrect.
        #
        # Every critical section it guards is a short, purely in-memory dict
        # write; the synchronous DB persistence is performed OUTSIDE the lock
        # (see run_all_diagnostics/run_category) so no reader ever blocks
        # behind disk I/O.
        # Constructed EAGERLY, never lazily. A lazy `if self.__lock is None:
        # self.__lock = threading.Lock()` property is itself a race: two
        # concurrent FIRST callers can both observe None and each construct
        # their OWN Lock, after which each believes it holds "the" lock while
        # actually holding a different one -- silently defeating every bit of
        # synchronisation below.
        self.__lock: threading.Lock = threading.Lock()

        # Story #1491: the compare-and-set token for get_status()'s
        # publish-back. Deliberately NOT the cache timestamp: datetime.now()
        # is not collision-free (a 2,000,000-sample probe observed genuine
        # duplicates), and a collision makes two DIFFERENT writes share a
        # token, so the CAS reads "unchanged" and clobbers the newer one -- a
        # textbook ABA. A monotonic counter cannot repeat a value, so an
        # unchanged token proves nothing was published in between. Bumped
        # under self.__lock at every publish site; timestamps remain in use
        # for TTL expiry, which is what they are actually good for.
        self._generation_counter = itertools.count(1)
        self._cache_generation: Dict[DiagnosticCategory, int] = {}

        self._backend = storage_backend

        # Database path for persistence
        if db_path is not None:
            self._db_path = db_path
        else:
            server_data_dir = os.environ.get(
                "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
            )
            self._db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")

        # Connection manager for the diagnostics database
        self._conn_manager = DatabaseConnectionManager.get_instance(self._db_path)

        # Load persisted results from database on initialization
        self._load_results_from_db()

    @property
    def _lock(self) -> threading.Lock:
        """The single, eagerly-constructed shared-state guard.

        Deliberately NOT lazily constructed -- see __init__.
        """
        return self.__lock

    def is_running(self) -> bool:
        """Check if any diagnostics are currently running.

        Story #1491 AC4: read under the lock. Both flags are written by
        background runs executing on Starlette threadpool workers, so an
        unsynchronised read here can observe a torn view across the two
        (e.g. _running already cleared while _running_categories has not yet
        been updated). Same discipline as get_status().
        """
        with self._lock:
            return self._running or len(self._running_categories) > 0

    def get_status(self) -> Dict[DiagnosticCategory, List[DiagnosticResult]]:
        """
        Get current diagnostic status for all categories.

        Returns cached results if available and not expired.
        If cache is empty/expired, tries to load from database.
        Otherwise returns placeholder NOT_RUN results and caches them.

        Returns:
            Dict mapping categories to their diagnostic results
        """
        now = datetime.now()
        status: Dict[DiagnosticCategory, List[DiagnosticResult]] = {}

        # Story #1491 AC4: ONE consistent snapshot of both shared dicts, taken
        # under the same cross-thread lock the write paths take. Reading them
        # unsynchronised (as this method used to) can observe a half-published
        # generation -- run_all_diagnostics/run_category publish _cache[category]
        # and _cache_timestamps[category] as a pair, and a reader interleaving
        # between the two sees new results stamped with the previous run's time
        # (or a missing stamp), i.e. a torn read across two dicts. Those runs are
        # now sync Starlette background tasks on threadpool workers, so this is a
        # genuine cross-thread race, not a theoretical one.
        with self._lock:
            cached_results = dict(self._cache)
            cached_timestamps = dict(self._cache_timestamps)
            # Captured in the SAME critical section as the data above, so the
            # generation observed here is exactly the one that produced this
            # snapshot -- that is what makes the later compare-and-set sound.
            cached_generations = dict(self._cache_generation)

        stale_categories: List[DiagnosticCategory] = []
        for category in DiagnosticCategory:
            # Get category-specific TTL
            cache_ttl = (
                API_CACHE_TTL
                if category == DiagnosticCategory.EXTERNAL_APIS
                else DEFAULT_CACHE_TTL
            )

            # Check if we have cached results that are still valid
            if (
                category in cached_results
                and category in cached_timestamps
                and now - cached_timestamps[category] < cache_ttl
            ):
                status[category] = cached_results[category]
            else:
                stale_categories.append(category)

        for category in stale_categories:
            # Cache is empty or expired - try to load from database.
            #
            # Story #1491 AC4: the database read is deliberately performed with
            # the lock RELEASED. self._lock is a cross-thread threading.Lock;
            # holding it across disk I/O would make every concurrent reader and
            # every in-flight diagnostics run block on that I/O -- exactly the
            # defect this story removed from the persistence step in
            # run_all_diagnostics, and it must not reappear on the read side.
            loaded = self._read_category_from_db(category)
            if loaded is not None:
                results, loaded_at = loaded
            else:
                # No DB results - generate and cache placeholder results
                results, loaded_at = self._get_placeholder_results(category), now

            # Story #1491 AC4: COMPARE-AND-SET, never an unconditional write.
            #
            # The DB read above deliberately ran with the lock released, so a
            # concurrent run_all_diagnostics/run_category -- now a genuine
            # cross-thread possibility, since both are sync Starlette
            # background tasks on threadpool workers -- may have published
            # FRESHER results for this category in the meantime. Overwriting
            # unconditionally would silently discard them (a lost update in
            # which the newest data loses).
            #
            # So: re-acquire, and publish only if this category's timestamp is
            # still exactly the one observed in the snapshot above. If it
            # changed, someone published while we were reading -- their value
            # is newer than our DB snapshot by construction, so we keep theirs
            # and return it.
            # The CAS token is the monotonic per-category generation, NOT the
            # timestamp: wall-clock values genuinely collide, and a collision
            # would read as "unchanged" and clobber a newer write (ABA).
            observed_generation = cached_generations.get(category)
            with self._lock:
                current_generation = self._cache_generation.get(category)
                if current_generation == observed_generation:
                    self._cache[category] = results
                    self._cache_timestamps[category] = loaded_at
                    self._cache_generation[category] = next(self._generation_counter)
                    status[category] = results
                else:
                    # Someone published while we were reading. Their value is
                    # newer than our DB snapshot by construction, so keep it.
                    # .get(..., results) rather than [category]: clear_cache()
                    # can remove the entry concurrently, and a status read must
                    # never raise KeyError over it.
                    status[category] = self._cache.get(category, results)

        return status

    def _get_placeholder_results(
        self, category: DiagnosticCategory
    ) -> List[DiagnosticResult]:
        """
        Get placeholder diagnostic results for a category.

        These are stub results that show NOT_RUN status.
        Actual diagnostic implementations will replace these in stories S3-S6.

        Args:
            category: The diagnostic category

        Returns:
            List of placeholder DiagnosticResult objects
        """
        placeholders = {
            DiagnosticCategory.CLI_TOOLS: [
                DiagnosticResult(
                    name="CIDX CLI",
                    status=DiagnosticStatus.NOT_RUN,
                    message="Diagnostic not yet implemented",
                    details={},
                )
            ],
            DiagnosticCategory.SDK_PREREQUISITES: [
                DiagnosticResult(
                    name="Python SDK",
                    status=DiagnosticStatus.NOT_RUN,
                    message="Diagnostic not yet implemented",
                    details={},
                )
            ],
            DiagnosticCategory.EXTERNAL_APIS: [
                DiagnosticResult(
                    name="VoyageAI API",
                    status=DiagnosticStatus.NOT_RUN,
                    message="Diagnostic not yet implemented",
                    details={},
                )
            ],
            DiagnosticCategory.CREDENTIALS: [
                DiagnosticResult(
                    name="GitHub Token",
                    status=DiagnosticStatus.NOT_RUN,
                    message="Diagnostic not yet implemented",
                    details={},
                )
            ],
            DiagnosticCategory.INFRASTRUCTURE: [
                DiagnosticResult(
                    name="Server Health",
                    status=DiagnosticStatus.NOT_RUN,
                    message="Diagnostic not yet implemented",
                    details={},
                )
            ],
        }

        return placeholders.get(category, [])

    async def run_all_diagnostics(self) -> None:
        """
        Run diagnostics for all categories.

        Executes actual diagnostic methods for each category and caches results.
        Uses exception isolation to ensure all categories run independently -
        if one category fails with an exception, other categories still execute.

        Sets running flag while executing.
        """
        import logging

        logger = logging.getLogger(__name__)

        with self._lock:
            self._running = True
        try:
            # Run diagnostics for each category with exception isolation
            for category in DiagnosticCategory:
                try:
                    if category == DiagnosticCategory.CLI_TOOLS:
                        results = await self.run_cli_tool_diagnostics()
                    elif category == DiagnosticCategory.SDK_PREREQUISITES:
                        results = await self.run_sdk_diagnostics()
                    elif category == DiagnosticCategory.EXTERNAL_APIS:
                        results = await self.run_external_api_diagnostics()
                    elif category == DiagnosticCategory.CREDENTIALS:
                        results = await self.run_credential_diagnostics()
                    elif category == DiagnosticCategory.INFRASTRUCTURE:
                        results = await self.run_infrastructure_diagnostics()
                    else:
                        # Fallback for unknown categories
                        results = self._get_placeholder_results(category)
                except Exception as e:
                    # Log error but continue with other categories
                    logger.error(f"Diagnostic category {category.value} failed: {e}")
                    results = [
                        DiagnosticResult(
                            name=f"{category.value} diagnostics",
                            status=DiagnosticStatus.ERROR,
                            message=f"Category diagnostic failed: {str(e)}",
                            details={"error_type": type(e).__name__},
                        )
                    ]

                # Store results in cache (always executes, even after exception)
                now = datetime.now()
                with self._lock:
                    self._cache[category] = results
                    self._cache_timestamps[category] = now
                    # Advance the CAS token so a concurrent get_status() can
                    # see that this publish happened.
                    self._cache_generation[category] = next(self._generation_counter)
                # Story #1491 AC4: persistence is deliberately OUTSIDE the
                # lock. self._lock is a cross-thread threading.Lock, and this
                # run executes on a worker thread -- holding it across a
                # synchronous SQLite write would make any concurrent
                # status/read path block on disk I/O. The in-memory cache is
                # already updated above, so a reader never waits on the write.
                self._save_results_to_db(category, results)

        finally:
            with self._lock:
                self._running = False

    def run_all_diagnostics_sync(self) -> None:
        """Synchronous entry point for the Starlette background task (AC4).

        Story #1491 AC4 (report Finding B4): Starlette AWAITS an async
        BackgroundTask on the event loop, so registering the
        ``run_all_diagnostics`` coroutine directly meant its synchronous SQLite
        writes and per-repo/per-collection filesystem calls froze every other
        connection for the whole run. Starlette instead runs a SYNC background
        task in its threadpool, so this plain ``def`` wrapper moves the entire
        run off the event loop.

        The coroutine itself is unchanged and runs on a private loop owned by
        this worker thread. That is safe because the shared-state guard is a
        threading.Lock (see ``__init__``) rather than a loop-bound
        asyncio.Lock, and every awaitable the run touches (httpx.AsyncClient,
        asyncio subprocesses) is created inside the run rather than shared
        across loops.
        """
        asyncio.run(self.run_all_diagnostics())

    def run_category_sync(self, category: DiagnosticCategory) -> None:
        """Synchronous entry point for the per-category background task (AC4).

        Same rationale and the same safety argument as
        ``run_all_diagnostics_sync``; the story requires both registrations in
        ``routers/diagnostics.py`` to leave the event loop.
        """
        asyncio.run(self.run_category(category))

    async def run_category(self, category: DiagnosticCategory) -> None:
        """
        Run diagnostics for a single category.

        Args:
            category: The category to run diagnostics for

        Clears cache for the specified category.
        """
        # Get category-specific TTL
        cache_ttl = (
            API_CACHE_TTL
            if category == DiagnosticCategory.EXTERNAL_APIS
            else DEFAULT_CACHE_TTL
        )

        # Check if cache is still valid.
        #
        # Story #1491 AC4: taken as ONE locked snapshot rather than three
        # separate unsynchronised reads. A concurrent background run publishes
        # _cache[category] and _cache_timestamps[category] as a pair, so
        # interleaving between these reads can observe one without the other
        # and mis-decide whether a re-run is needed. Same discipline as
        # get_status().
        now = datetime.now()
        with self._lock:
            cached_at = self._cache_timestamps.get(category)
            has_results = category in self._cache
        if has_results and cached_at is not None and now - cached_at < cache_ttl:
            return  # Cache is valid, no need to re-run

        with self._lock:
            self._running_categories.add(category)
        try:
            # Run category-specific diagnostics
            if category == DiagnosticCategory.INFRASTRUCTURE:
                results = await self.run_infrastructure_diagnostics()
            elif category == DiagnosticCategory.CLI_TOOLS:
                results = await self.run_cli_tool_diagnostics()
            elif category == DiagnosticCategory.SDK_PREREQUISITES:
                results = await self.run_sdk_diagnostics()
            elif category == DiagnosticCategory.EXTERNAL_APIS:
                results = await self.run_external_api_diagnostics()
            elif category == DiagnosticCategory.CREDENTIALS:
                results = await self.run_credential_diagnostics()
            else:
                # Placeholder for other categories (implemented in other stories)
                await asyncio.sleep(0.1)
                results = self._get_placeholder_results(category)

            # Store results in cache for this category
            with self._lock:
                self._cache[category] = results
                self._cache_timestamps[category] = datetime.now()
                # Advance the CAS token: this publish must be visible to a
                # concurrent get_status() publish-back check.
                self._cache_generation[category] = next(self._generation_counter)

            # Persist to database
            self._save_results_to_db(category, results)

        finally:
            with self._lock:
                self._running_categories.discard(category)

    def get_category_status(
        self, category: DiagnosticCategory
    ) -> List[DiagnosticResult]:
        """
        Get diagnostic status for a specific category.

        Args:
            category: The category to get status for

        Returns:
            List of diagnostic results for the category
        """
        all_status = self.get_status()
        return all_status.get(category, [])

    def is_category_running(self, category: DiagnosticCategory) -> bool:
        """
        Check if a specific category is currently running.

        Args:
            category: The category to check

        Returns:
            True if category diagnostics are running
        """
        # Story #1491: read under the lock. _running_categories is mutated by
        # background runs on Starlette threadpool workers, so an unsynchronised
        # membership test can observe a partially-updated set. Same discipline
        # as is_running() and get_status().
        with self._lock:
            return category in self._running_categories

    async def check_cli_tool(
        self,
        name: str,
        command: str,
        required_sdk: Optional[str] = None,
        sdk_available: Optional[Dict[str, bool]] = None,
    ) -> DiagnosticResult:
        """
        Check single CLI tool availability and version.

        Args:
            name: Human-readable tool name
            command: Command to execute (e.g., "rg --version")
            required_sdk: Optional SDK key that this tool depends on
            sdk_available: Optional dict mapping SDK keys to availability

        Returns:
            DiagnosticResult with tool status
        """
        # Check SDK dependency first
        if (
            required_sdk
            and sdk_available
            and not sdk_available.get(required_sdk, False)
        ):
            return DiagnosticResult(
                name=name,
                status=DiagnosticStatus.NOT_APPLICABLE,
                message=f"{name} requires {required_sdk} SDK which is not available",
                details={"required_sdk": required_sdk},
            )

        # Parse command to get program name
        parts = command.split()
        program = parts[0]
        args = parts[1:] if len(parts) > 1 else []

        try:
            # Execute command with timeout
            process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    program,
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=DIAGNOSTIC_TIMEOUT_SECONDS,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=DIAGNOSTIC_TIMEOUT_SECONDS
            )

            # Check exit code
            if process.returncode != 0:
                return DiagnosticResult(
                    name=name,
                    status=DiagnosticStatus.ERROR,
                    message=f"{name} command failed with exit code {process.returncode}",
                    details={
                        "exit_code": process.returncode,
                        "stderr": stderr.decode("utf-8", errors="replace").strip(),
                    },
                )

            # Extract version from output
            output = stdout.decode("utf-8", errors="replace").strip()
            version = self._extract_version(output)

            return DiagnosticResult(
                name=name,
                status=DiagnosticStatus.WORKING,
                message=f"{name} is installed and working",
                details={"version": version, "command": command},
            )

        except FileNotFoundError:
            return DiagnosticResult(
                name=name,
                status=DiagnosticStatus.NOT_CONFIGURED,
                message=f"{name} not found - tool is not installed or not in PATH",
                details={"command": command},
            )
        except asyncio.TimeoutError:
            return DiagnosticResult(
                name=name,
                status=DiagnosticStatus.ERROR,
                message=f"{name} check timed out after {DIAGNOSTIC_TIMEOUT_SECONDS} seconds",
                details={"timeout_seconds": DIAGNOSTIC_TIMEOUT_SECONDS},
            )
        except Exception as e:
            return DiagnosticResult(
                name=name,
                status=DiagnosticStatus.ERROR,
                message=f"Unexpected error checking {name}: {str(e)}",
                details={"error_type": type(e).__name__},
            )

    def _extract_version(self, output: str) -> str:
        """
        Extract version number from command output.

        Supports various formats:
        - "tool 1.2.3"
        - "tool version 1.2.3"
        - "version: 1.2.3"

        Args:
            output: Command stdout output

        Returns:
            Extracted version string or "unknown"
        """
        # Try to match common version patterns (semver, date-based)
        patterns = [
            r"(\d+\.\d+\.\d+)",  # 1.2.3
            r"(\d+\.\d+)",  # 1.2
            r"version\s+(\d+\.\d+\.\d+)",  # version 1.2.3
            r"v(\d+\.\d+\.\d+)",  # v1.2.3
        ]

        for pattern in patterns:
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                return match.group(1)

        return "unknown"

    async def run_cli_tool_diagnostics(self) -> List[DiagnosticResult]:
        """
        Run all CLI tool dependency checks in parallel.

        First checks SDK availability, then checks CLI tools with SDK dependency mapping.

        Returns:
            List of DiagnosticResult objects for all CLI tools
        """
        # First, check SDK availability for dependency mapping
        sdk_results = await self.run_sdk_diagnostics()
        sdk_available = {
            "dotnet": any(
                r.name == ".NET SDK" and r.status == DiagnosticStatus.WORKING
                for r in sdk_results
            ),
            "go": any(
                r.name == "Go SDK" and r.status == DiagnosticStatus.WORKING
                for r in sdk_results
            ),
            "nodejs": any(
                r.name == "Node.js/npm" and r.status == DiagnosticStatus.WORKING
                for r in sdk_results
            ),
        }

        # Run all CLI tool checks in parallel
        tasks = [
            self.check_cli_tool(
                tool["name"],  # type: ignore[index]
                tool["command"],  # type: ignore[index]
                required_sdk=tool["required_sdk"],  # type: ignore[index]
                sdk_available=sdk_available,
            )
            for tool in CLI_TOOLS
        ]

        results = await asyncio.gather(*tasks)
        return list(results)

    async def run_sdk_diagnostics(self) -> List[DiagnosticResult]:
        """
        Run all SDK prerequisite checks in parallel.

        Returns:
            List of DiagnosticResult objects for all SDKs
        """
        # Run all SDK checks in parallel
        tasks = [
            self.check_cli_tool(sdk["name"], sdk["command"])
            for sdk in SDK_PREREQUISITES
        ]

        results = await asyncio.gather(*tasks)
        return list(results)

    def clear_cache(self, category: Optional[DiagnosticCategory] = None) -> None:
        """
        Clear cached results.

        Args:
            category: If provided, clear only this category. Otherwise clear all.
        """
        # Story #1491: under the lock, like every other mutation of these
        # dicts. This was the last unlocked mutator; racing the deliberately
        # unlocked DB read in get_status() is exactly what made that method's
        # publish-back able to observe a vanished key.
        #
        # The generation map is cleared alongside, so a later publish starts
        # from "no observed generation" rather than comparing against a token
        # whose data no longer exists.
        with self._lock:
            if category:
                self._cache.pop(category, None)
                self._cache_timestamps.pop(category, None)
                self._cache_generation.pop(category, None)
            else:
                self._cache.clear()
                self._cache_timestamps.clear()
                self._cache_generation.clear()

    def _save_results_to_db(
        self, category: DiagnosticCategory, results: List[DiagnosticResult]
    ) -> None:
        """
        Save diagnostic results to database.

        Uses INSERT OR REPLACE to overwrite existing results for the category.
        Stores results as JSON array with timestamp.

        Args:
            category: The diagnostic category
            results: List of DiagnosticResult objects to save
        """
        try:
            # Serialize results to JSON
            results_json = json.dumps([r.to_dict() for r in results])
            run_at = datetime.now().isoformat()

            if self._backend is not None:
                self._backend.save_results(category.value, results_json, run_at)
                return

            # Save to database
            def _do_save(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "INSERT OR REPLACE INTO diagnostic_results (category, results_json, run_at) VALUES (?, ?, ?)",
                    (category.value, results_json, run_at),
                )

            self._conn_manager.execute_atomic(_do_save)

        except Exception as e:
            # Log error but don't fail diagnostic execution
            import logging

            logger = logging.getLogger(__name__)
            logger.error(f"Failed to save diagnostic results to database: {e}")

    def _load_results_from_db(self) -> None:
        """
        Load persisted diagnostic results from database on initialization.

        Populates the cache with previously saved results. If database
        is empty or has errors, cache remains empty (will use placeholders).
        """
        try:
            if self._backend is not None:
                rows = self._backend.load_all_results()
            else:
                # Check if database file exists
                if not Path(self._db_path).exists():
                    return

                # Bug #1532 follow-up: route the raw connection through
                # guarded_connection() so close_all() cannot close it
                # mid-read.
                with self._conn_manager.guarded_connection() as conn:
                    cursor = conn.execute(
                        "SELECT category, results_json, run_at FROM diagnostic_results"
                    )
                    rows = cursor.fetchall()

            for row in rows:
                category_str, results_json, run_at = row

                # Parse category
                try:
                    category = DiagnosticCategory(category_str)
                except ValueError:
                    continue  # Skip unknown categories

                # Deserialize results
                results_data = json.loads(results_json)
                results = []

                for result_dict in results_data:
                    # Reconstruct DiagnosticResult from dict
                    result = DiagnosticResult(
                        name=result_dict["name"],
                        status=DiagnosticStatus(result_dict["status"]),
                        message=result_dict["message"],
                        details=result_dict.get("details", {}),
                        timestamp=datetime.fromisoformat(result_dict["timestamp"]),
                    )
                    results.append(result)

                # Populate cache
                self._cache[category] = results
                self._cache_timestamps[category] = datetime.fromisoformat(run_at)

        except Exception as e:
            # Log error but don't fail service initialization
            import logging

            logger = logging.getLogger(__name__)
            logger.error(f"Failed to load diagnostic results from database: {e}")

    def _read_category_from_db(
        self, category: DiagnosticCategory
    ) -> Optional[Tuple[List[DiagnosticResult], datetime]]:
        """
        Read persisted results for a single category from the database.

        Story #1491 AC4: this is a PURE READ -- it never touches the shared
        _cache/_cache_timestamps dicts. Publication is the caller's job, done
        under the shared-state lock, so this method (the only disk I/O on the
        status path) can safely run with that cross-thread lock released.

        Args:
            category: The diagnostic category to read

        Returns:
            (results, run_at) if the category has persisted results, else None
        """
        try:
            if self._backend is not None:
                result_row = self._backend.load_category_results(category.value)
                if result_row is None:
                    return None
                results_json, run_at = result_row
            else:
                # Check if database file exists
                if not Path(self._db_path).exists():
                    return None

                # Bug #1532 follow-up: route the raw connection through
                # guarded_connection() so close_all() cannot close it
                # mid-read.
                with self._conn_manager.guarded_connection() as conn:
                    cursor = conn.execute(
                        "SELECT results_json, run_at FROM diagnostic_results WHERE category = ?",
                        (category.value,),
                    )
                    db_row = cursor.fetchone()

                if db_row is None:
                    return None

                results_json, run_at = db_row

            # Deserialize results
            results_data = json.loads(results_json)
            results = []

            for result_dict in results_data:
                # Reconstruct DiagnosticResult from dict
                result = DiagnosticResult(
                    name=result_dict["name"],
                    status=DiagnosticStatus(result_dict["status"]),
                    message=result_dict["message"],
                    details=result_dict.get("details", {}),
                    timestamp=datetime.fromisoformat(result_dict["timestamp"]),
                )
                results.append(result)

            return results, datetime.fromisoformat(run_at)

        except Exception as e:
            # Log error but report "nothing persisted" so the caller falls back
            # to placeholder results, exactly as before.
            import logging

            logger = logging.getLogger(__name__)
            logger.error(f"Failed to load category {category.value} from database: {e}")
            return None

    def _get_token_manager(self) -> CITokenManager:
        """
        Get CITokenManager configured for SQLite backend.

        Returns:
            CITokenManager instance configured with SQLite backend using
            the same database path as the diagnostics service.
        """
        from code_indexer.server.services.ci_token_manager import create_token_manager

        server_dir = str(Path(self._db_path).parent.parent)
        return create_token_manager(
            server_dir=server_dir,
            db_path=self._db_path,
        )

    async def check_vector_storage(self) -> DiagnosticResult:
        """
        Check vector storage health by validating HNSW indexes across all golden repos.

        Bug #147: Actually validates HNSW indexes can be loaded, not just directory existence.

        Scans ~/.cidx-server/data/golden-repos/{repo}/.code-indexer/index/ for:
        - Code semantic indexes (voyage-code-3, voyage-3-large, etc.)
        - Temporal indexes (code-indexer-temporal)

        Validates each index by attempting to load it with HNSWIndexManager.

        Returns:
            DiagnosticResult with per-repo validation status and aggregates
        """
        try:
            # Get storage path from config
            from code_indexer.server.services.config_service import get_config_service

            config = get_config_service().get_config()
            storage_path = Path(config.server_dir) / "data"
            golden_repos_path = storage_path / "golden-repos"

            # Check if storage directory exists
            if not storage_path.exists():
                return DiagnosticResult(
                    name="Vector Storage",
                    status=DiagnosticStatus.NOT_CONFIGURED,
                    message="Vector storage directory not configured or missing",
                    details={"expected_path": str(storage_path), "repos_checked": 0},
                )

            # Check if directory is readable
            if not os.access(storage_path, os.R_OK):
                return DiagnosticResult(
                    name="Vector Storage",
                    status=DiagnosticStatus.ERROR,
                    message="Vector storage directory is unreadable - permission denied",
                    details={"path": str(storage_path), "repos_checked": 0},
                )

            # Check if golden-repos directory exists
            if not golden_repos_path.exists():
                return DiagnosticResult(
                    name="Vector Storage",
                    status=DiagnosticStatus.NOT_CONFIGURED,
                    message="No golden repositories directory found",
                    details={
                        "path": str(storage_path),
                        "repos_checked": 0,
                    },
                )

            # Validate HNSW indexes across all golden repos
            validation_results = self._validate_hnsw_indexes(golden_repos_path)

            # Determine overall status
            if validation_results["repos_checked"] == 0:
                status = DiagnosticStatus.NOT_CONFIGURED
                message = "No golden repositories found"
            elif len(validation_results["repos_with_issues"]) > 0:
                # If ANY repo has issues, report ERROR
                status = DiagnosticStatus.ERROR
                message = f"Found {len(validation_results['repos_with_issues'])} repositories with HNSW index issues"
            else:
                status = DiagnosticStatus.WORKING
                message = f"All {validation_results['repos_with_healthy_indexes']} repositories have healthy HNSW indexes"

            return DiagnosticResult(
                name="Vector Storage",
                status=status,
                message=message,
                details={
                    "path": str(storage_path),
                    **validation_results,
                },
            )

        except PermissionError:
            return DiagnosticResult(
                name="Vector Storage",
                status=DiagnosticStatus.ERROR,
                message="Permission denied accessing storage directory",
                details={"repos_checked": 0},
            )
        except Exception as e:
            return DiagnosticResult(
                name="Vector Storage",
                status=DiagnosticStatus.ERROR,
                message=f"Unexpected error checking storage: {str(e)}",
                details={"error_type": type(e).__name__, "repos_checked": 0},
            )

    def _validate_hnsw_indexes(self, golden_repos_path: Path) -> Dict[str, Any]:
        """
        Validate HNSW indexes across all golden repositories.

        Bug #149 Fix: Query golden_repos_metadata database table to get list of
        registered repos, then ONLY validate those repos. Do NOT scan random
        filesystem directories like "aliases", "cidx-meta", etc.

        Scans each repo's .code-indexer/index/ directory for collection subdirectories,
        delegates validation to _check_collection_health().

        Args:
            golden_repos_path: Path to golden-repos directory

        Returns:
            Dictionary with validation results including:
            - repos_checked: Number of repos scanned
            - repos_with_healthy_indexes: Number of repos with all indexes loadable
            - repos_with_issues: List of {repo, issue} dicts for problematic repos
            - index_types_found: Set of index collection names discovered
        """
        repos_checked = 0
        repos_with_healthy_indexes = 0
        repos_with_issues = []
        index_types_found = set()

        # Bug #149 Fix: Query database for registered golden repos instead of scanning filesystem
        try:
            # Bug #1532 follow-up: route the raw connection through
            # guarded_connection() so close_all() cannot close it mid-read.
            with self._conn_manager.guarded_connection() as conn:
                cursor = conn.execute(
                    "SELECT alias, clone_path FROM golden_repos_metadata"
                )
                registered_repos = cursor.fetchall()
        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.error(f"Failed to query registered repos from database: {e}")
            # Return empty results if database query fails
            return {
                "repos_checked": 0,
                "repos_with_healthy_indexes": 0,
                "repos_with_issues": [],
                "index_types_found": [],
            }

        # If no registered repos, return early
        if not registered_repos:
            return {
                "repos_checked": 0,
                "repos_with_healthy_indexes": 0,
                "repos_with_issues": [],
                "index_types_found": [],
            }

        # Iterate over registered repos from database (NOT filesystem directories)
        for alias, clone_path in registered_repos:
            repos_checked += 1
            repo_name = alias
            repo_has_issues = False

            # Bug #172 Fix: Resolve actual repository path (handles versioned structure)
            # Database clone_path may be stale if repo uses .versioned/{alias}/v_*/ structure
            actual_repo_dir = Path(clone_path)

            # Check if .versioned/{alias}/ exists in golden-repos directory
            versioned_base = golden_repos_path / ".versioned" / alias
            if versioned_base.exists() and versioned_base.is_dir():
                # Find all v_* subdirectories (format: v_TIMESTAMP)
                version_dirs = []
                try:
                    for entry in versioned_base.iterdir():
                        if entry.is_dir() and entry.name.startswith("v_"):
                            try:
                                # Validate format: v_TIMESTAMP (extract and parse timestamp)
                                timestamp = int(entry.name.split("_")[1])
                                version_dirs.append((entry, timestamp))
                            except (ValueError, IndexError):
                                # Skip malformed version directories (e.g., v_, v_abc)
                                continue
                except Exception:
                    # If scanning fails, fall back to clone_path
                    pass

                # Use latest version if any valid versions found
                if version_dirs:
                    # Sort by timestamp (highest = latest)
                    version_dirs.sort(key=lambda x: x[1], reverse=True)
                    actual_repo_dir = version_dirs[0][0]

            # Check for .code-indexer/index/ directory
            index_base_path = actual_repo_dir / ".code-indexer" / "index"
            if not index_base_path.exists() or not index_base_path.is_dir():
                repos_with_issues.append(
                    {
                        "repo": repo_name,
                        "issue": "Missing .code-indexer/index directory",
                    }
                )
                continue

            # Scan for collection directories (voyage-code-3, code-indexer-temporal, etc.)
            collections_found = 0
            for collection_dir in index_base_path.iterdir():
                if not collection_dir.is_dir():
                    continue

                collection_name = collection_dir.name
                collections_found += 1
                index_types_found.add(collection_name)

                # Validate this collection's health
                collection_issue = self._check_collection_health(
                    collection_dir, repo_name, collection_name
                )
                if collection_issue:
                    repos_with_issues.append(collection_issue)
                    repo_has_issues = True

            # GitHub Issue #1482 extension: local-clone/general-versioned-
            # snapshot discovery above cannot see a temporal shard
            # relocated to the golden-owned sister location (Story #1457
            # AC1) -- that sister root lives under a DIFFERENT namespace
            # (.versioned/{alias}-temporal-{slug}-{quarter}/v_*/), never
            # nested under actual_repo_dir. Reuses the SAME resolver-aware
            # helper the repository_health_aggregator.py fix (Bug #1482
            # extension site 2) established -- never a parallel sister
            # scan. Entirely fail-open: any resolution error leaves this
            # repo's local-scan-only result unchanged.
            try:
                from code_indexer.server.services.repository_health_aggregator import (
                    discover_sister_temporal_collections,
                )

                sister_collections = discover_sister_temporal_collections(
                    golden_repos_path, alias, index_base_path
                )
            except Exception as exc:
                import logging

                logging.getLogger(__name__).warning(
                    "_validate_hnsw_indexes: sister-relocated temporal "
                    "discovery failed for repo %s (isolated, non-fatal): %s",
                    repo_name,
                    exc,
                )
                sister_collections = []

            for collection_name, _index_type, hnsw_file in sister_collections:
                if collection_name in index_types_found:
                    continue
                collections_found += 1
                index_types_found.add(collection_name)

                collection_issue = self._check_collection_health(
                    hnsw_file.parent, repo_name, collection_name
                )
                if collection_issue:
                    repos_with_issues.append(collection_issue)
                    repo_has_issues = True

            # If repo had no collections, that's an issue
            if collections_found == 0:
                repos_with_issues.append(
                    {"repo": repo_name, "issue": "No index collections found"}
                )
                repo_has_issues = True

            # Track healthy repos
            if not repo_has_issues:
                repos_with_healthy_indexes += 1

        return {
            "repos_checked": repos_checked,
            "repos_with_healthy_indexes": repos_with_healthy_indexes,
            "repos_with_issues": repos_with_issues,
            "index_types_found": sorted(list(index_types_found)),
        }

    def _check_collection_health(
        self, collection_dir: Path, repo_name: str, collection_name: str
    ) -> Optional[Dict[str, str]]:
        """
        Check health of a single HNSW index collection.

        Validates:
        - hnsw_index.bin file exists
        - collection_meta.json file exists and is valid JSON
        - HNSW index can be loaded without errors

        Args:
            collection_dir: Path to collection directory
            repo_name: Repository name (for error reporting)
            collection_name: Collection name (for error reporting)

        Returns:
            Dict with {repo, issue} if unhealthy, None if healthy
        """
        # Bug #188 fix: Detect temporal collections (FilesystemVectorStore format)
        # Temporal collections have temporal_metadata.db, not hnsw_index.bin
        temporal_metadata_file = collection_dir / "temporal_metadata.db"
        if temporal_metadata_file.exists():
            # This is a temporal collection - validate FilesystemVectorStore format
            meta_file = collection_dir / "collection_meta.json"
            projection_file = collection_dir / "projection_matrix.npy"

            if not meta_file.exists():
                return {
                    "repo": repo_name,
                    "issue": f"Missing collection_meta.json in temporal collection {collection_name}",
                }

            if not projection_file.exists():
                return {
                    "repo": repo_name,
                    "issue": f"Missing projection_matrix.npy in temporal collection {collection_name}",
                }

            # Validate metadata is valid JSON
            try:
                with open(meta_file, "r") as f:
                    json.load(f)
            except json.JSONDecodeError:
                return {
                    "repo": repo_name,
                    "issue": f"Corrupted metadata JSON in temporal collection {collection_name}",
                }

            # Temporal collection is healthy
            return None

        # Non-temporal collection - validate HNSW format
        hnsw_file = collection_dir / "hnsw_index.bin"
        meta_file = collection_dir / "collection_meta.json"

        if not hnsw_file.exists():
            # Check if this is an empty collection (legitimately has no HNSW file)
            if meta_file.exists():
                try:
                    with open(meta_file, "r") as f:
                        metadata = json.load(f)

                    # Get vector count from metadata (if hnsw_index section exists)
                    vector_count = metadata.get("hnsw_index", {}).get(
                        "vector_count", None
                    )
                    unique_file_count = metadata.get("unique_file_count", None)

                    # Empty collection is healthy - no HNSW file expected
                    # Three indicators of empty collection:
                    # 1. vector_count == 0 (finalized empty collection)
                    # 2. unique_file_count == 0 (no files indexed)
                    # 3. No hnsw_index section at all (never finalized)
                    if (
                        vector_count == 0
                        or unique_file_count == 0
                        or "hnsw_index" not in metadata
                    ):
                        import logging

                        logger = logging.getLogger(__name__)
                        logger.debug(
                            f"Empty collection '{collection_name}' in repo '{repo_name}' "
                            f"(vector_count={vector_count}, unique_file_count={unique_file_count}, "
                            f"hnsw_index_exists={'hnsw_index' in metadata}, no HNSW file) - skipping health check"
                        )
                        return None

                except (json.JSONDecodeError, KeyError, ValueError):
                    # Corrupted metadata will be caught later
                    pass

            # Missing HNSW file with nonzero vectors (or no metadata) is a real problem
            return {
                "repo": repo_name,
                "issue": f"Missing HNSW index file in {collection_name}",
            }

        if not meta_file.exists():
            return {
                "repo": repo_name,
                "issue": f"Missing metadata file in {collection_name}",
            }

        # Attempt to load the HNSW index to verify it's not corrupted
        try:
            # Read metadata to get vector_dim
            with open(meta_file, "r") as f:
                metadata = json.load(f)

            vector_dim = metadata.get("vector_size") or metadata.get(
                "hnsw_index", {}
            ).get("vector_dim", 1024)

            # Try to load the index
            manager = HNSWIndexManager(vector_dim=vector_dim, space="cosine")
            loaded_index = manager.load_index(
                collection_dir, max_elements=DEFAULT_HNSW_MAX_ELEMENTS
            )

            if loaded_index is None:
                return {
                    "repo": repo_name,
                    "issue": f"Failed to load HNSW index in {collection_name}",
                }

        except json.JSONDecodeError:
            return {
                "repo": repo_name,
                "issue": f"Corrupted metadata JSON in {collection_name}",
            }
        except Exception as e:
            return {
                "repo": repo_name,
                "issue": f"Error loading HNSW index in {collection_name}: {str(e)}",
            }

        # Collection is healthy
        return None

    def _create_db_error_result(
        self, message: str, details: Optional[Dict[str, Any]] = None
    ) -> DiagnosticResult:
        """
        Create error result for SQLite database diagnostic.

        Args:
            message: Error message
            details: Optional additional details

        Returns:
            DiagnosticResult with ERROR status
        """
        return DiagnosticResult(
            name="SQLite Database",
            status=DiagnosticStatus.ERROR,
            message=message,
            details=details or {},
        )

    def _check_database_schema(
        self, conn: sqlite3.Connection
    ) -> Tuple[bool, List[str]]:
        """
        Validate database schema has all required tables.

        Args:
            conn: Open SQLite database connection

        Returns:
            Tuple of (schema_valid, missing_tables)
        """
        # Bug #187 fix: Removed groups, repo_group_access, audit_logs
        # These tables are in groups.db (separate database), not cidx_server.db
        required_tables = [
            "users",
            "user_api_keys",
            "user_mcp_credentials",
            "golden_repos_metadata",
            "global_repos",
        ]

        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = {row[0] for row in cursor.fetchall()}

        missing_tables = [
            table for table in required_tables if table not in existing_tables
        ]

        return len(missing_tables) == 0, missing_tables

    async def run_infrastructure_diagnostics(self) -> List[DiagnosticResult]:
        """
        Run all infrastructure diagnostic checks.

        Returns:
            List of DiagnosticResult objects for infrastructure components
        """
        results = []

        # Story #1491 AC4 -- why these two asyncio.wait_for wrappers stay, and
        # why no preemption mechanism is added at this layer.
        #
        # Both wrapped coroutines are today entirely SYNCHRONOUS internally
        # (check_sqlite_database: SQLite connect + PRAGMA integrity_check;
        # check_vector_storage: per-repo/per-collection filesystem stats, reads
        # and HNSW loads -- neither contains a single await). A wait_for around
        # a coroutine that never yields cannot fire, so these deadlines bound
        # nothing at present. That is DELIBERATE, not an oversight:
        #
        #   * The starvation hazard report Finding B4 identified was this run
        #     executing ON the event loop. That is fixed at the entry point --
        #     run_all_diagnostics_sync / run_category_sync are sync Starlette
        #     background tasks, so the whole run happens on a threadpool worker
        #     and no other connection waits on it. Nothing is starved by a slow
        #     check here, so there is nothing for a deadline at THIS layer to
        #     protect.
        #   * Making one fire would mean offloading the synchronous body to yet
        #     another thread purely so the wrapper had something cancellable --
        #     and cancelling it could not stop the work anyway (Python cannot
        #     interrupt a thread blocked in a syscall), so it would abandon a
        #     live worker while reporting a timeout. That is the very
        #     "wait_for cannot interrupt synchronous work" anti-pattern this
        #     story documents, not a fix for it.
        #
        # They are retained rather than deleted because they become live, correct
        # protection the moment either check gains genuinely awaitable work
        # (e.g. an HTTP or async-subprocess probe), which is also why the
        # asyncio.TimeoutError branches below are kept intact.
        try:
            db_result = await asyncio.wait_for(
                self.check_sqlite_database(), timeout=DIAGNOSTIC_TIMEOUT_SECONDS
            )
            results.append(db_result)
        except asyncio.TimeoutError:
            results.append(
                self._create_db_error_result(
                    f"Database check timed out after {DIAGNOSTIC_TIMEOUT_SECONDS} seconds"
                )
            )
        except Exception as e:
            results.append(self._create_db_error_result(f"Unexpected error: {str(e)}"))

        try:
            # Story #1491 AC4: same rationale as the block above -- this deadline
            # cannot fire while check_vector_storage's body has no await, and is
            # kept for the day it does. The run itself is already off the event
            # loop, so a slow scan starves nothing.
            storage_result = await asyncio.wait_for(
                self.check_vector_storage(), timeout=DIAGNOSTIC_TIMEOUT_SECONDS
            )
            results.append(storage_result)
        except asyncio.TimeoutError:
            results.append(
                DiagnosticResult(
                    name="Vector Storage",
                    status=DiagnosticStatus.ERROR,
                    message=f"Storage check timed out after {DIAGNOSTIC_TIMEOUT_SECONDS} seconds",
                    details={},
                )
            )
        except Exception as e:
            results.append(
                DiagnosticResult(
                    name="Vector Storage",
                    status=DiagnosticStatus.ERROR,
                    message=f"Unexpected error: {str(e)}",
                    details={},
                )
            )

        return results

    async def check_github_api(self) -> DiagnosticResult:
        """
        Check GitHub API connectivity and authentication.

        Returns:
            DiagnosticResult with GitHub API status
        """
        try:
            # Get token from CITokenManager
            token_manager = self._get_token_manager()
            token_data = token_manager.get_token("github")

            if token_data is None:
                return DiagnosticResult(
                    name="GitHub API",
                    status=DiagnosticStatus.NOT_CONFIGURED,
                    message="GitHub API token not configured",
                    details={},
                )

            # Make API call with timeout
            async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    "https://api.github.com/rate_limit",
                    headers={"Authorization": f"Bearer {token_data.token}"},
                )
                response.raise_for_status()
                data = response.json()

                return DiagnosticResult(
                    name="GitHub API",
                    status=DiagnosticStatus.WORKING,
                    message="GitHub API is accessible",
                    details={"rate_limit": data.get("rate", {})},
                )
        except httpx.TimeoutException:
            return DiagnosticResult(
                name="GitHub API",
                status=DiagnosticStatus.ERROR,
                message="GitHub API request timed out",
                details={},
            )
        except httpx.HTTPStatusError as e:
            return DiagnosticResult(
                name="GitHub API",
                status=DiagnosticStatus.ERROR,
                message=f"GitHub API request failed: {e.response.status_code}",
                details={"status_code": e.response.status_code},
            )
        except Exception as e:
            return DiagnosticResult(
                name="GitHub API",
                status=DiagnosticStatus.ERROR,
                message=f"GitHub API error: {str(e)}",
                details={"error_type": type(e).__name__},
            )

    async def check_gitlab_api(self) -> DiagnosticResult:
        """
        Check GitLab API connectivity and authentication.

        Returns:
            DiagnosticResult with GitLab API status
        """
        try:
            # Get token from CITokenManager
            token_manager = self._get_token_manager()
            token_data = token_manager.get_token("gitlab")

            if token_data is None:
                return DiagnosticResult(
                    name="GitLab API",
                    status=DiagnosticStatus.NOT_CONFIGURED,
                    message="GitLab API token not configured",
                    details={},
                )

            # Build API URL from base_url
            api_url = f"{token_data.base_url}/api/v4/user"

            # Make API call with timeout
            async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    api_url, headers={"Authorization": f"Bearer {token_data.token}"}
                )
                response.raise_for_status()
                data = response.json()

                return DiagnosticResult(
                    name="GitLab API",
                    status=DiagnosticStatus.WORKING,
                    message="GitLab API is accessible",
                    details={"username": data.get("username", "unknown")},
                )
        except httpx.TimeoutException:
            return DiagnosticResult(
                name="GitLab API",
                status=DiagnosticStatus.ERROR,
                message="GitLab API request timed out",
                details={},
            )
        except httpx.HTTPStatusError as e:
            return DiagnosticResult(
                name="GitLab API",
                status=DiagnosticStatus.ERROR,
                message=f"GitLab API request failed: {e.response.status_code}",
                details={"status_code": e.response.status_code},
            )
        except Exception as e:
            return DiagnosticResult(
                name="GitLab API",
                status=DiagnosticStatus.ERROR,
                message=f"GitLab API error: {str(e)}",
                details={"error_type": type(e).__name__},
            )

    async def check_oidc_provider(self) -> DiagnosticResult:
        """
        Check OIDC Provider connectivity via discovery endpoint.

        Returns:
            DiagnosticResult with OIDC Provider status
        """
        try:
            # Get OIDC config
            from code_indexer.server.services.config_service import get_config_service

            config = get_config_service().get_config()

            if not config.oidc_provider_config.enabled:
                return DiagnosticResult(
                    name="OIDC Provider",
                    status=DiagnosticStatus.NOT_CONFIGURED,
                    message="OIDC Provider not configured",
                    details={},
                )

            # Build discovery endpoint URL
            discovery_url = f"{config.oidc_provider_config.issuer_url}/.well-known/openid-configuration"

            # Make API call with timeout
            async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
                response = await client.get(discovery_url)
                response.raise_for_status()
                data = response.json()

                return DiagnosticResult(
                    name="OIDC Provider",
                    status=DiagnosticStatus.WORKING,
                    message="OIDC Provider is accessible",
                    details={"issuer": data.get("issuer", "unknown")},
                )
        except httpx.TimeoutException:
            return DiagnosticResult(
                name="OIDC Provider",
                status=DiagnosticStatus.ERROR,
                message="OIDC Provider request timed out",
                details={},
            )
        except httpx.HTTPStatusError as e:
            return DiagnosticResult(
                name="OIDC Provider",
                status=DiagnosticStatus.ERROR,
                message=f"OIDC Provider request failed: {e.response.status_code}",
                details={"status_code": e.response.status_code},
            )
        except Exception as e:
            return DiagnosticResult(
                name="OIDC Provider",
                status=DiagnosticStatus.ERROR,
                message=f"OIDC Provider error: {str(e)}",
                details={"error_type": type(e).__name__},
            )

    async def check_otel_collector(self) -> DiagnosticResult:
        """
        Check OpenTelemetry Collector connectivity.

        Returns:
            DiagnosticResult with OTEL Collector status
        """
        try:
            # Get telemetry config
            from code_indexer.server.services.config_service import get_config_service

            config = get_config_service().get_config()

            if not config.telemetry_config.enabled:
                return DiagnosticResult(
                    name="OpenTelemetry Collector",
                    status=DiagnosticStatus.NOT_CONFIGURED,
                    message="OpenTelemetry Collector not configured",
                    details={},
                )

            # Test collector endpoint (simple connectivity check)
            collector_url = config.telemetry_config.collector_endpoint

            # Make API call with timeout
            async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
                response = await client.get(collector_url)
                response.raise_for_status()

                return DiagnosticResult(
                    name="OpenTelemetry Collector",
                    status=DiagnosticStatus.WORKING,
                    message="OpenTelemetry Collector is accessible",
                    details={},
                )
        except httpx.TimeoutException:
            return DiagnosticResult(
                name="OpenTelemetry Collector",
                status=DiagnosticStatus.ERROR,
                message="OpenTelemetry Collector request timed out",
                details={},
            )
        except httpx.ConnectError:
            return DiagnosticResult(
                name="OpenTelemetry Collector",
                status=DiagnosticStatus.ERROR,
                message="OpenTelemetry Collector connection failed",
                details={},
            )
        except httpx.HTTPStatusError as e:
            return DiagnosticResult(
                name="OpenTelemetry Collector",
                status=DiagnosticStatus.ERROR,
                message=f"OpenTelemetry Collector request failed: {e.response.status_code}",
                details={"status_code": e.response.status_code},
            )
        except Exception as e:
            return DiagnosticResult(
                name="OpenTelemetry Collector",
                status=DiagnosticStatus.ERROR,
                message=f"OpenTelemetry Collector error: {str(e)}",
                details={"error_type": type(e).__name__},
            )

    async def run_external_api_diagnostics(self) -> List[DiagnosticResult]:
        """
        Run all external API diagnostic checks in parallel.

        Returns:
            List of DiagnosticResult objects for all external APIs
        """
        # Execute all API checks in parallel using asyncio.gather
        results = await asyncio.gather(
            self.check_github_api(),
            self.check_gitlab_api(),
            self.check_oidc_provider(),
            self.check_otel_collector(),
        )

        return list(results)

    def _parse_ssh_output(self, output: str, returncode: int) -> DiagnosticResult:
        """
        Parse SSH command output and return appropriate DiagnosticResult.

        Args:
            output: Combined stdout/stderr from SSH command
            returncode: Process exit code

        Returns:
            DiagnosticResult based on authentication status
        """
        # Check for successful authentication
        # GitHub: "successfully authenticated" in stderr, exit code 1
        # GitLab: "Welcome to GitLab" in output, exit code 0
        if (
            "successfully authenticated" in output.lower()
            or "welcome to gitlab" in output.lower()
        ):
            return DiagnosticResult(
                name="SSH Keys",
                status=DiagnosticStatus.WORKING,
                message="SSH authentication successful",
                details={"output": output.strip()},
            )

        # Check for permission denied (no key or wrong key)
        if "permission denied" in output.lower():
            return DiagnosticResult(
                name="SSH Keys",
                status=DiagnosticStatus.ERROR,
                message="SSH permission denied - no valid key configured",
                details={"exit_code": returncode, "output": output.strip()},
            )

        # Check for host key verification failure
        if "host key verification failed" in output.lower():
            return DiagnosticResult(
                name="SSH Keys",
                status=DiagnosticStatus.ERROR,
                message="SSH host key verification failed",
                details={"exit_code": returncode, "output": output.strip()},
            )

        # Other non-zero exit codes
        if returncode != 0:
            return DiagnosticResult(
                name="SSH Keys",
                status=DiagnosticStatus.ERROR,
                message=f"SSH command failed with exit code {returncode}",
                details={"exit_code": returncode, "output": output.strip()},
            )

        # Unexpected success without authentication message
        return DiagnosticResult(
            name="SSH Keys",
            status=DiagnosticStatus.ERROR,
            message="SSH command completed but authentication unclear",
            details={"exit_code": returncode, "output": output.strip()},
        )

    async def check_ssh_keys(self) -> DiagnosticResult:
        """
        Check SSH key connectivity to GitHub/GitLab (Story S5 AC1, AC6, AC8).

        Runs `ssh -T git@github.com` to test SSH key authentication.

        Returns:
            DiagnosticResult with SSH key status:
            - WORKING: Successfully authenticated
            - ERROR: Permission denied, timeout, or other failure
            - NOT_CONFIGURED: SSH not installed
        """
        try:
            # Execute SSH test command with timeout (AC6: 60 seconds)
            process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    "ssh",
                    "-T",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "BatchMode=yes",
                    "git@github.com",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=SSH_TIMEOUT_SECONDS,
            )

            stdout, stderr = await process.communicate()

            # Decode output
            stderr_text = stderr.decode("utf-8", errors="replace")
            stdout_text = stdout.decode("utf-8", errors="replace")
            output = stderr_text or stdout_text

            # Parse output and return result
            return self._parse_ssh_output(output, process.returncode)  # type: ignore[arg-type]

        except asyncio.TimeoutError:
            return DiagnosticResult(
                name="SSH Keys",
                status=DiagnosticStatus.ERROR,
                message=f"SSH check timed out after {SSH_TIMEOUT_SECONDS} seconds",
                details={"timeout_seconds": SSH_TIMEOUT_SECONDS},
            )
        except FileNotFoundError:
            # SSH not installed (AC8: NOT_CONFIGURED)
            return DiagnosticResult(
                name="SSH Keys",
                status=DiagnosticStatus.NOT_CONFIGURED,
                message="SSH not found - SSH is not installed or not in PATH",
                details={},
            )
        except Exception as e:
            return DiagnosticResult(
                name="SSH Keys",
                status=DiagnosticStatus.ERROR,
                message=f"Unexpected error checking SSH keys: {str(e)}",
                details={"error_type": type(e).__name__},
            )

    async def check_github_token(self) -> DiagnosticResult:
        """
        Check GitHub API token format and connectivity (Story S5 AC2, AC7, AC8).

        Validates token format AND tests API call.

        Returns:
            DiagnosticResult with GitHub token status:
            - WORKING: Valid format and API call succeeds
            - WARNING: Invalid format
            - ERROR: API call fails
            - NOT_CONFIGURED: No token configured
        """
        try:
            # Get token from CITokenManager
            token_manager = self._get_token_manager()
            token_data = token_manager.get_token("github")

            if token_data is None:
                return DiagnosticResult(
                    name="GitHub Token",
                    status=DiagnosticStatus.NOT_CONFIGURED,
                    message="GitHub API token not configured",
                    details={},
                )

            # Validate token format (AC2: validate format AND test API)
            if not GITHUB_TOKEN_PATTERN.match(token_data.token):
                return DiagnosticResult(
                    name="GitHub Token",
                    status=DiagnosticStatus.WARNING,
                    message="GitHub token has invalid format (expected ghp_* or github_pat_*)",
                    details={
                        "token_prefix": (
                            token_data.token[:10] if len(token_data.token) >= 10 else ""
                        )
                    },
                )

            # Test API call with timeout (AC7: 30 seconds)
            async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {token_data.token}"},
                )
                response.raise_for_status()
                data = response.json()

                return DiagnosticResult(
                    name="GitHub Token",
                    status=DiagnosticStatus.WORKING,
                    message="GitHub API token is valid and working",
                    details={"username": data.get("login", "unknown")},
                )

        except httpx.TimeoutException:
            return DiagnosticResult(
                name="GitHub Token",
                status=DiagnosticStatus.ERROR,
                message="GitHub API request timed out",
                details={},
            )
        except httpx.HTTPStatusError as e:
            return DiagnosticResult(
                name="GitHub Token",
                status=DiagnosticStatus.ERROR,
                message=f"GitHub API request failed: {e.response.status_code}",
                details={"status_code": e.response.status_code},
            )
        except Exception as e:
            return DiagnosticResult(
                name="GitHub Token",
                status=DiagnosticStatus.ERROR,
                message=f"GitHub API error: {str(e)}",
                details={"error_type": type(e).__name__},
            )

    async def check_gitlab_token(self) -> DiagnosticResult:
        """
        Check GitLab API token format and connectivity (Story S5 AC3).

        Validates token format AND tests API call.

        Returns:
            DiagnosticResult with GitLab token status:
            - WORKING: Valid format and API call succeeds
            - WARNING: Invalid format
            - ERROR: API call fails
            - NOT_CONFIGURED: No token configured
        """
        try:
            # Get token from CITokenManager
            token_manager = self._get_token_manager()
            token_data = token_manager.get_token("gitlab")

            if token_data is None:
                return DiagnosticResult(
                    name="GitLab Token",
                    status=DiagnosticStatus.NOT_CONFIGURED,
                    message="GitLab API token not configured",
                    details={},
                )

            # Validate token format (AC3: validate format AND test API)
            if not GITLAB_TOKEN_PATTERN.match(token_data.token):
                return DiagnosticResult(
                    name="GitLab Token",
                    status=DiagnosticStatus.WARNING,
                    message="GitLab token has invalid format (expected glpat-*)",
                    details={
                        "token_prefix": (
                            token_data.token[:10] if len(token_data.token) >= 10 else ""
                        )
                    },
                )

            # Build API URL from base_url
            api_url = f"{token_data.base_url}/api/v4/user"

            # Test API call with timeout (AC7: 30 seconds)
            async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    api_url, headers={"Authorization": f"Bearer {token_data.token}"}
                )
                response.raise_for_status()
                data = response.json()

                return DiagnosticResult(
                    name="GitLab Token",
                    status=DiagnosticStatus.WORKING,
                    message="GitLab API token is valid and working",
                    details={"username": data.get("username", "unknown")},
                )

        except httpx.TimeoutException:
            return DiagnosticResult(
                name="GitLab Token",
                status=DiagnosticStatus.ERROR,
                message="GitLab API request timed out",
                details={},
            )
        except httpx.HTTPStatusError as e:
            return DiagnosticResult(
                name="GitLab Token",
                status=DiagnosticStatus.ERROR,
                message=f"GitLab API request failed: {e.response.status_code}",
                details={"status_code": e.response.status_code},
            )
        except Exception as e:
            return DiagnosticResult(
                name="GitLab Token",
                status=DiagnosticStatus.ERROR,
                message=f"GitLab API error: {str(e)}",
                details={"error_type": type(e).__name__},
            )

    async def run_credential_diagnostics(self) -> List[DiagnosticResult]:
        """
        Run all credential diagnostic checks in parallel (Story S5 AC5).

        Executes all 3 credential checks using asyncio.gather:
        - SSH Keys
        - GitHub Token
        - GitLab Token

        Returns:
            List of DiagnosticResult objects for all credential checks
        """
        results = await asyncio.gather(
            self.check_ssh_keys(),
            self.check_github_token(),
            self.check_gitlab_token(),
        )
        return list(results)

    async def check_sqlite_database(self) -> DiagnosticResult:
        """
        Check SQLite database health.

        Validates:
        - Database file exists
        - Can connect to database
        - Database integrity is OK
        - Required tables exist

        Returns:
            DiagnosticResult with database status
        """
        try:
            # Get database path from config
            from code_indexer.server.services.config_service import get_config_service

            config = get_config_service().get_config()
            db_path = Path(config.server_dir) / "data" / "cidx_server.db"

            # Check if database file exists
            if not db_path.exists():
                return self._create_db_error_result(
                    "Database file not found", {"expected_path": str(db_path)}
                )

            # Try to connect and validate
            try:
                conn = DatabaseConnectionManager.get_instance(
                    str(db_path)
                ).get_connection()
                # Check database integrity
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check(1)")
                integrity_result = cursor.fetchone()[0]

                if integrity_result != "ok":
                    return self._create_db_error_result(
                        f"Database integrity check failed: {integrity_result}"
                    )

                # Check schema
                schema_valid, missing_tables = self._check_database_schema(conn)
                if not schema_valid:
                    return self._create_db_error_result(
                        f"Database schema incomplete: missing {len(missing_tables)} table(s)",
                        {"missing_tables": missing_tables},
                    )

                # Get file size
                file_size = db_path.stat().st_size

                return DiagnosticResult(
                    name="SQLite Database",
                    status=DiagnosticStatus.WORKING,
                    message="Database is healthy and accessible",
                    details={
                        "path": str(db_path),
                        "size_bytes": file_size,
                        "integrity": "ok",
                        "schema_valid": True,
                    },
                )
            except PermissionError:
                return self._create_db_error_result(
                    "Permission denied accessing database file"
                )
        except sqlite3.DatabaseError as e:
            # Check if this is a permission error
            error_msg = str(e).lower()
            if "unable to open database file" in error_msg:
                return self._create_db_error_result(
                    "Permission denied accessing database file"
                )
            return self._create_db_error_result(
                f"Database error: {str(e)}", {"error_type": "DatabaseError"}
            )
        except Exception as e:
            return self._create_db_error_result(
                f"Unexpected error checking database: {str(e)}",
                {"error_type": type(e).__name__},
            )
