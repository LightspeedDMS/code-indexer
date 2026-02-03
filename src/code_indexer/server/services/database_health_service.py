"""
Database Health Service for monitoring all 7 central databases.

Story #712: Dashboard Refinements - Database Health Honeycomb
Story #3: Removed search_config.db and file_content_limits.db (migrated to config.json)
         Added scip_audit.db for SCIP indexing audit tracking

Implements 5-point health checks per database:
1. Connect - Open SQLite connection
2. Read - SELECT from any table
3. Write - INSERT/UPDATE to _health_check table
4. Quick Integrity - PRAGMA quick_check
5. Not Locked - Check for exclusive locks
"""

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Story #30 AC2: Cache TTL in seconds
CACHE_TTL_SECONDS = 60

logger = logging.getLogger(__name__)

# Story #30 Bug Fix: Module-level singleton instance
# The cache must be shared across all callers. Creating new DatabaseHealthService()
# instances on each request means the instance-level cache is always empty.
# This singleton ensures the cache persists and is shared.
_db_health_service_instance: Optional["DatabaseHealthService"] = None


def get_database_health_service(
    server_dir: Optional[str] = None,
) -> "DatabaseHealthService":
    """
    Get the singleton DatabaseHealthService instance.

    Story #30 Bug Fix: This function ensures the cache is shared across all
    callers. Previously, creating new DatabaseHealthService() instances on
    each request meant the instance-level cache was always empty.

    Args:
        server_dir: Path to server data directory. Only used on first call
                   to create the singleton. Ignored on subsequent calls.

    Returns:
        The singleton DatabaseHealthService instance
    """
    global _db_health_service_instance
    if _db_health_service_instance is None:
        _db_health_service_instance = DatabaseHealthService(server_dir)
    return _db_health_service_instance


def _reset_singleton_for_testing() -> None:
    """
    Reset the singleton instance for testing purposes.

    This allows tests to start with a fresh instance and clean cache.
    Should only be used in test code, never in production.
    """
    global _db_health_service_instance
    _db_health_service_instance = None


def _format_file_size(size_bytes: int) -> str:
    """
    Format a file size in bytes to a human-readable string.

    Args:
        size_bytes: Size in bytes

    Returns:
        Formatted string like "512 B", "45.2 KB", "128.5 MB", or "1.25 GB"
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


class DatabaseHealthStatus(str, Enum):
    """Health status levels for database health checks."""

    HEALTHY = "healthy"  # Green - All 5 checks pass
    WARNING = "warning"  # Yellow - Some checks pass, some fail
    ERROR = "error"  # Red - Critical checks fail


@dataclass
class CheckResult:
    """Result of a single health check."""

    passed: bool
    error_message: Optional[str] = None


@dataclass
class DatabaseHealthResult:
    """Complete health check result for a single database."""

    file_name: str
    display_name: str
    status: DatabaseHealthStatus
    checks: Dict[str, CheckResult]
    db_path: str = ""  # Full path to database file

    def get_tooltip(self) -> str:
        """
        Get tooltip text for honeycomb hover.

        Always shows display name and path.
        Shows file size if file exists.
        Unhealthy databases also show the failed condition.
        """
        # Always include path in tooltip
        lines = [self.display_name, self.db_path]

        # Add file size if file exists
        try:
            size_bytes = os.path.getsize(self.db_path)
            lines.append(f"Size: {_format_file_size(size_bytes)}")
        except (FileNotFoundError, OSError):
            # File doesn't exist or can't be accessed - omit size line
            pass

        if self.status == DatabaseHealthStatus.HEALTHY:
            return "\n".join(lines)

        # Find first failed check to include in tooltip
        for check_name, result in self.checks.items():
            if not result.passed:
                check_display = check_name.replace("_", " ").title()
                error_info = result.error_message or "failed"
                lines.append(f"{check_display}: {error_info}")
                break

        return "\n".join(lines)


# Database file to display name mapping (central server databases only)
# Story #3: Removed search_config.db and file_content_limits.db (migrated to config.json)
# Added scip_audit.db for SCIP indexing audit tracking
DATABASE_DISPLAY_NAMES: Dict[str, str] = {
    "cidx_server.db": "Main Server",
    "oauth.db": "OAuth",
    "refresh_tokens.db": "Refresh Tokens",
    "logs.db": "Logs",
    "groups.db": "Groups",
    "scip_audit.db": "SCIP Audit",
    "payload_cache.db": "Payload Cache",
}


class DatabaseHealthService:
    """
    Service for checking health of all 7 central CIDX databases.

    Performs 5-point health checks on each database and determines
    overall status (healthy/warning/error).
    """

    def __init__(self, server_dir: Optional[str] = None):
        """
        Initialize the database health service.

        Args:
            server_dir: Path to server data directory. If None, uses
                       CIDX_SERVER_DATA_DIR env var or ~/.cidx-server
        """
        if server_dir:
            self.server_dir = Path(server_dir)
        else:
            self.server_dir = Path(
                os.environ.get(
                    "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
                )
            )

        # Story #30 AC2: Initialize cache for database health results
        # Cache structure: {db_path: (DatabaseHealthResult, timestamp)}
        self._health_cache: Dict[str, Tuple[DatabaseHealthResult, float]] = {}
        self._cache_lock = threading.Lock()

    def check_database_health_cached(
        self, db_path: str, display_name: str = "Unknown"
    ) -> DatabaseHealthResult:
        """
        Check database health with caching (Story #30 AC2).

        Returns cached result if within 60-second TTL, otherwise
        performs fresh check and updates cache.

        Args:
            db_path: Path to SQLite database file
            display_name: Human-readable name for display

        Returns:
            DatabaseHealthResult with health check results
        """
        current_time = time.time()

        with self._cache_lock:
            # Check if we have a valid cached result
            if db_path in self._health_cache:
                cached_result, cached_time = self._health_cache[db_path]
                if current_time - cached_time < CACHE_TTL_SECONDS:
                    return cached_result

        # Cache miss or expired - perform fresh check
        result = self.check_database_health(db_path, display_name)

        with self._cache_lock:
            self._health_cache[db_path] = (result, current_time)

        return result

    def get_all_database_health(self) -> List[DatabaseHealthResult]:
        """
        Check health of all 7 central databases (uncached).

        Returns:
            List of DatabaseHealthResult for each database
        """
        results = []

        for file_name, display_name in DATABASE_DISPLAY_NAMES.items():
            # Determine correct path based on database location
            if file_name == "cidx_server.db":
                # Main server DB is in data/ subdirectory
                db_path = self.server_dir / "data" / file_name
            elif file_name == "payload_cache.db":
                # Payload cache is in golden-repos cache directory
                db_path = (
                    self.server_dir / "data" / "golden-repos" / ".cache" / file_name
                )
            else:
                # All other databases are in server root
                db_path = self.server_dir / file_name

            result = self.check_database_health(str(db_path), display_name)
            results.append(result)

        return results

    def get_all_database_health_cached(self) -> List[DatabaseHealthResult]:
        """
        Check health of all 7 central databases with caching (Story #30 AC6).

        Uses check_database_health_cached for each database, returning
        cached results when within 60-second TTL.

        Returns:
            List of DatabaseHealthResult for each database
        """
        results = []

        for file_name, display_name in DATABASE_DISPLAY_NAMES.items():
            # Determine correct path based on database location
            if file_name == "cidx_server.db":
                # Main server DB is in data/ subdirectory
                db_path = self.server_dir / "data" / file_name
            elif file_name == "payload_cache.db":
                # Payload cache is in golden-repos cache directory
                db_path = (
                    self.server_dir / "data" / "golden-repos" / ".cache" / file_name
                )
            else:
                # All other databases are in server root
                db_path = self.server_dir / file_name

            result = self.check_database_health_cached(str(db_path), display_name)
            results.append(result)

        return results

    @staticmethod
    def check_database_health(
        db_path: str, display_name: str = "Unknown"
    ) -> DatabaseHealthResult:
        """
        Perform 5-point health check on a single database.

        Args:
            db_path: Path to SQLite database file
            display_name: Human-readable name for display

        Returns:
            DatabaseHealthResult with all check results
        """
        file_name = Path(db_path).name
        checks: Dict[str, CheckResult] = {}

        # Check 1: Connect
        checks["connect"] = DatabaseHealthService._check_connect(db_path)

        if checks["connect"].passed:
            # Check 2: Read (only if connect succeeded)
            checks["read"] = DatabaseHealthService._check_read(db_path)

            # Check 3: Write (only if connect succeeded)
            checks["write"] = DatabaseHealthService._check_write(db_path)

            # Check 4: Integrity (only if connect succeeded)
            checks["integrity"] = DatabaseHealthService._check_integrity(db_path)

            # Check 5: Not Locked (only if connect succeeded)
            checks["not_locked"] = DatabaseHealthService._check_not_locked(db_path)
        else:
            # If connect failed, all other checks fail too
            checks["read"] = CheckResult(
                passed=False, error_message="Connection required"
            )
            checks["write"] = CheckResult(
                passed=False, error_message="Connection required"
            )
            checks["integrity"] = CheckResult(
                passed=False, error_message="Connection required"
            )
            checks["not_locked"] = CheckResult(
                passed=False, error_message="Connection required"
            )

        # Determine overall status
        status = DatabaseHealthService._determine_status(checks)

        return DatabaseHealthResult(
            file_name=file_name,
            display_name=display_name,
            status=status,
            checks=checks,
            db_path=db_path,
        )

    @staticmethod
    def _check_connect(db_path: str) -> CheckResult:
        """Check 1: Can we connect to the database?"""
        try:
            # Check if file exists first
            if not Path(db_path).exists():
                return CheckResult(
                    passed=False, error_message="Connection failed: file not found"
                )

            with sqlite3.connect(db_path, timeout=5) as conn:
                # Simple test to verify connection works
                conn.execute("SELECT 1")
            return CheckResult(passed=True)
        except Exception as e:
            return CheckResult(passed=False, error_message=f"Connection failed: {e}")

    @staticmethod
    def _check_read(db_path: str) -> CheckResult:
        """Check 2: Can we read from the database?"""
        try:
            with sqlite3.connect(db_path, timeout=5) as conn:
                # Read sqlite_master to verify read capability
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' LIMIT 1"
                )
            return CheckResult(passed=True)
        except Exception as e:
            return CheckResult(passed=False, error_message=f"Read failed: {e}")

    @staticmethod
    def _check_write(db_path: str) -> CheckResult:
        """
        Check 3: Can we write to the database?

        Uses _health_check table with INSERT OR REPLACE pattern.

        Migration Strategy Note:
            This method uses CREATE TABLE IF NOT EXISTS instead of a versioned
            migration for the _health_check table. This is intentional because:

            1. Runtime health check tables are operational metadata, not application
               data. They don't require migration versioning or schema evolution.

            2. The _health_check table is trivial (single row, single timestamp)
               and will never need schema changes.

            3. CREATE TABLE IF NOT EXISTS provides idempotency - the health check
               can run safely on any database without prior setup.

            4. This pattern keeps the health service self-contained and avoids
               coupling to the migration system for purely operational concerns.
        """
        try:
            with sqlite3.connect(db_path, timeout=5) as conn:
                # Create _health_check table if not exists (see docstring for rationale)
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS _health_check (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        last_check TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )"""
                )

                # Update or insert health check record
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT OR REPLACE INTO _health_check (id, last_check) VALUES (1, ?)",
                    (now,),
                )
                conn.commit()
            return CheckResult(passed=True)
        except Exception as e:
            return CheckResult(passed=False, error_message=f"Write failed: {e}")

    @staticmethod
    def _check_integrity(db_path: str) -> CheckResult:
        """Check 4: Does PRAGMA integrity_check(1) pass?

        Story #30 AC1: Uses integrity_check(1) which only checks the first
        page of the database for performance. This reduces check time from
        85+ seconds to milliseconds for large databases like logs.db.
        """
        try:
            with sqlite3.connect(db_path, timeout=5) as conn:
                cursor = conn.execute("PRAGMA integrity_check(1)")
                result = cursor.fetchone()
                if result and result[0] == "ok":
                    return CheckResult(passed=True)
                else:
                    return CheckResult(
                        passed=False,
                        error_message=f"Integrity check failed: {result[0] if result else 'unknown'}",
                    )
        except Exception as e:
            return CheckResult(
                passed=False, error_message=f"Integrity check failed: {e}"
            )

    @staticmethod
    def _check_not_locked(db_path: str) -> CheckResult:
        """Check 5: Is the database not exclusively locked?"""
        try:
            with sqlite3.connect(db_path, timeout=1) as conn:
                # Try to acquire a shared lock
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            return CheckResult(passed=True)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                return CheckResult(passed=False, error_message="Database locked")
            return CheckResult(passed=False, error_message=f"Lock check failed: {e}")
        except Exception as e:
            return CheckResult(passed=False, error_message=f"Lock check failed: {e}")

    @staticmethod
    def _determine_status(checks: Dict[str, CheckResult]) -> DatabaseHealthStatus:
        """
        Determine overall health status from individual check results.

        - GREEN (HEALTHY): All 5 checks pass
        - YELLOW (WARNING): Some checks pass, some fail (degraded but operational)
        - RED (ERROR): Critical checks fail (connect/read)
        """
        # Critical checks - if these fail, status is ERROR
        critical_checks = ["connect", "read"]
        for check_name in critical_checks:
            if check_name in checks and not checks[check_name].passed:
                return DatabaseHealthStatus.ERROR

        # Check if all passed
        all_passed = all(result.passed for result in checks.values())
        if all_passed:
            return DatabaseHealthStatus.HEALTHY

        # Some non-critical checks failed
        return DatabaseHealthStatus.WARNING
