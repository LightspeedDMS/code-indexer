"""
Database connection pooling and schema management for SQLite storage.

Story #702: Migrate Central JSON Files to SQLite

Provides:
- DatabaseSchema: Creates and manages the SQLite schema for server state
"""

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Dict, Optional, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


class DatabaseSchema:
    """
    Manages SQLite database schema creation and initialization.

    Creates all tables required for storing server state that was previously
    stored in JSON files (global_registry.json, users.json, jobs.json, etc.).
    """

    # SQL statements for creating each table
    CREATE_GLOBAL_REPOS_TABLE = """
        CREATE TABLE IF NOT EXISTS global_repos (
            alias_name TEXT PRIMARY KEY,
            repo_name TEXT NOT NULL,
            repo_url TEXT,
            index_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_refresh TEXT NOT NULL,
            enable_temporal BOOLEAN DEFAULT FALSE,
            temporal_options TEXT,
            enable_scip BOOLEAN DEFAULT FALSE
        )
    """

    CREATE_USERS_TABLE = """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            email TEXT,
            created_at TEXT NOT NULL,
            oidc_identity TEXT
        )
    """

    CREATE_USER_API_KEYS_TABLE = """
        CREATE TABLE IF NOT EXISTS user_api_keys (
            key_id TEXT PRIMARY KEY,
            username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            key_hash TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            name TEXT,
            created_at TEXT NOT NULL
        )
    """

    CREATE_USER_MCP_CREDENTIALS_TABLE = """
        CREATE TABLE IF NOT EXISTS user_mcp_credentials (
            credential_id TEXT PRIMARY KEY,
            username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            client_id TEXT NOT NULL,
            client_secret_hash TEXT NOT NULL,
            client_id_prefix TEXT NOT NULL,
            name TEXT,
            created_at TEXT NOT NULL,
            last_used_at TEXT
        )
    """

    CREATE_USER_OIDC_IDENTITIES_TABLE = """
        CREATE TABLE IF NOT EXISTS user_oidc_identities (
            username TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
            subject TEXT NOT NULL,
            email TEXT,
            linked_at TEXT NOT NULL,
            last_login TEXT
        )
    """

    CREATE_SYNC_JOBS_TABLE = """
        CREATE TABLE IF NOT EXISTS sync_jobs (
            job_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            user_alias TEXT NOT NULL,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            repository_url TEXT,
            progress INTEGER DEFAULT 0,
            error_message TEXT,
            phases TEXT,
            phase_weights TEXT,
            current_phase TEXT,
            progress_history TEXT,
            recovery_checkpoint TEXT,
            analytics_data TEXT
        )
    """

    CREATE_CI_TOKENS_TABLE = """
        CREATE TABLE IF NOT EXISTS ci_tokens (
            platform TEXT PRIMARY KEY,
            encrypted_token TEXT NOT NULL,
            base_url TEXT
        )
    """

    CREATE_INVALIDATED_SESSIONS_TABLE = """
        CREATE TABLE IF NOT EXISTS invalidated_sessions (
            username TEXT NOT NULL,
            token_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (username, token_id)
        )
    """

    CREATE_PASSWORD_CHANGE_TIMESTAMPS_TABLE = """
        CREATE TABLE IF NOT EXISTS password_change_timestamps (
            username TEXT PRIMARY KEY,
            changed_at TEXT NOT NULL
        )
    """

    CREATE_SSH_KEYS_TABLE = """
        CREATE TABLE IF NOT EXISTS ssh_keys (
            name TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            key_type TEXT NOT NULL,
            private_path TEXT NOT NULL,
            public_path TEXT NOT NULL,
            public_key TEXT,
            email TEXT,
            description TEXT,
            created_at TEXT,
            imported_at TEXT,
            is_imported BOOLEAN DEFAULT FALSE
        )
    """

    CREATE_SSH_KEY_HOSTS_TABLE = """
        CREATE TABLE IF NOT EXISTS ssh_key_hosts (
            key_name TEXT NOT NULL REFERENCES ssh_keys(name) ON DELETE CASCADE,
            hostname TEXT NOT NULL,
            PRIMARY KEY (key_name, hostname)
        )
    """

    # Story #711: Golden Repository Metadata table
    CREATE_GOLDEN_REPOS_METADATA_TABLE = """
        CREATE TABLE IF NOT EXISTS golden_repos_metadata (
            alias TEXT PRIMARY KEY NOT NULL,
            repo_url TEXT NOT NULL,
            default_branch TEXT NOT NULL,
            clone_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            enable_temporal INTEGER NOT NULL DEFAULT 0,
            temporal_options TEXT
        )
    """

    # Background Jobs table (Bug fix: BackgroundJobManager SQLite migration)
    CREATE_BACKGROUND_JOBS_TABLE = """
        CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            error TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            username TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            repo_alias TEXT,
            resolution_attempts INTEGER NOT NULL DEFAULT 0,
            claude_actions TEXT,
            failure_reason TEXT,
            extended_error TEXT,
            language_resolution_status TEXT
        )
    """

    # Story #72: Self-Monitoring tables (Epic #71)
    CREATE_SELF_MONITORING_SCANS_TABLE = """
        CREATE TABLE IF NOT EXISTS self_monitoring_scans (
            scan_id TEXT PRIMARY KEY NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            log_id_start INTEGER NOT NULL,
            log_id_end INTEGER,
            issues_created INTEGER NOT NULL DEFAULT 0,
            error_message TEXT
        )
    """

    CREATE_SELF_MONITORING_ISSUES_TABLE = """
        CREATE TABLE IF NOT EXISTS self_monitoring_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT NOT NULL REFERENCES self_monitoring_scans(scan_id) ON DELETE CASCADE,
            github_issue_number INTEGER,
            github_issue_url TEXT,
            classification TEXT NOT NULL,
            title TEXT NOT NULL,
            error_codes TEXT,
            fingerprint TEXT NOT NULL,
            source_log_ids TEXT NOT NULL,
            source_files TEXT,
            created_at TEXT NOT NULL
        )
    """

    # Indexes for self-monitoring tables
    CREATE_SELF_MONITORING_SCANS_STARTED_AT_INDEX = """
        CREATE INDEX IF NOT EXISTS idx_self_monitoring_scans_started_at
        ON self_monitoring_scans(started_at)
    """

    CREATE_SELF_MONITORING_ISSUES_SCAN_ID_INDEX = """
        CREATE INDEX IF NOT EXISTS idx_self_monitoring_issues_scan_id
        ON self_monitoring_issues(scan_id)
    """

    # Story #141: Research Assistant tables
    CREATE_RESEARCH_SESSIONS_TABLE = """
        CREATE TABLE IF NOT EXISTS research_sessions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            claude_session_id TEXT
        )
    """

    CREATE_RESEARCH_MESSAGES_TABLE = """
        CREATE TABLE IF NOT EXISTS research_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES research_sessions(id) ON DELETE CASCADE
        )
    """

    CREATE_DIAGNOSTIC_RESULTS_TABLE = """
        CREATE TABLE IF NOT EXISTS diagnostic_results (
            category TEXT PRIMARY KEY,
            results_json TEXT NOT NULL,
            run_at TEXT NOT NULL
        )
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        """
        Initialize DatabaseSchema.

        Args:
            db_path: Path to SQLite database file. If None, uses default
                     location based on CIDX_SERVER_DATA_DIR or ~/.cidx-server/data/
        """
        if db_path is not None:
            self.db_path = db_path
        else:
            server_data_dir = os.environ.get(
                "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
            )
            self.db_path = str(Path(server_data_dir) / "data" / "cidx_server.db")

    def initialize_database(self) -> None:
        """
        Initialize the database with all required tables.

        Creates parent directories with secure permissions (0700) if they don't exist.
        Enables WAL mode for concurrent reads during writes.
        """
        db_path = Path(self.db_path)

        # Create parent directory with secure permissions
        parent_dir = db_path.parent
        if not parent_dir.exists():
            parent_dir.mkdir(parents=True, mode=0o700)
        else:
            # Ensure existing directory has secure permissions
            os.chmod(parent_dir, 0o700)

        # Create database and tables
        conn = sqlite3.connect(str(db_path))
        try:
            # Enable foreign keys
            conn.execute("PRAGMA foreign_keys = ON")

            # Enable WAL mode for concurrent reads
            conn.execute("PRAGMA journal_mode = WAL")

            # Create all tables
            conn.execute(self.CREATE_GLOBAL_REPOS_TABLE)
            conn.execute(self.CREATE_USERS_TABLE)
            conn.execute(self.CREATE_USER_API_KEYS_TABLE)
            conn.execute(self.CREATE_USER_MCP_CREDENTIALS_TABLE)
            conn.execute(self.CREATE_USER_OIDC_IDENTITIES_TABLE)
            conn.execute(self.CREATE_SYNC_JOBS_TABLE)
            conn.execute(self.CREATE_CI_TOKENS_TABLE)
            conn.execute(self.CREATE_INVALIDATED_SESSIONS_TABLE)
            conn.execute(self.CREATE_PASSWORD_CHANGE_TIMESTAMPS_TABLE)
            conn.execute(self.CREATE_SSH_KEYS_TABLE)
            conn.execute(self.CREATE_SSH_KEY_HOSTS_TABLE)
            conn.execute(self.CREATE_GOLDEN_REPOS_METADATA_TABLE)
            conn.execute(self.CREATE_BACKGROUND_JOBS_TABLE)
            # Story #72: Self-monitoring tables
            conn.execute(self.CREATE_SELF_MONITORING_SCANS_TABLE)
            conn.execute(self.CREATE_SELF_MONITORING_ISSUES_TABLE)
            # Story #72: Self-monitoring indexes
            conn.execute(self.CREATE_SELF_MONITORING_SCANS_STARTED_AT_INDEX)
            conn.execute(self.CREATE_SELF_MONITORING_ISSUES_SCAN_ID_INDEX)
            # Story #141: Research Assistant tables
            conn.execute(self.CREATE_RESEARCH_SESSIONS_TABLE)
            conn.execute(self.CREATE_RESEARCH_MESSAGES_TABLE)
            # Diagnostic results persistence
            conn.execute(self.CREATE_DIAGNOSTIC_RESULTS_TABLE)

            conn.commit()

            # Run schema migrations for existing databases
            self._migrate_self_monitoring_issues_schema(conn)
            self._migrate_global_repos_schema(conn)
            self._migrate_research_sessions_schema(conn)

            logger.info(f"Database initialized at {db_path}")

        finally:
            conn.close()

    def _migrate_self_monitoring_issues_schema(self, conn: sqlite3.Connection) -> None:
        """
        Migrate self_monitoring_issues table schema for existing databases.

        Adds columns that were added after the initial table creation:
        - error_codes: Error codes found in logs
        - fingerprint: Deduplication fingerprint
        - source_files: Source files involved

        This is safe to run multiple times - it only adds missing columns.
        """
        # Get existing columns
        cursor = conn.execute("PRAGMA table_info(self_monitoring_issues)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        migrations_applied = []

        # Add missing columns (order matters for NOT NULL with DEFAULT)
        if "error_codes" not in existing_columns:
            conn.execute(
                "ALTER TABLE self_monitoring_issues ADD COLUMN error_codes TEXT"
            )
            migrations_applied.append("error_codes")

        if "fingerprint" not in existing_columns:
            conn.execute(
                "ALTER TABLE self_monitoring_issues "
                "ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''"
            )
            migrations_applied.append("fingerprint")

        if "source_files" not in existing_columns:
            conn.execute(
                "ALTER TABLE self_monitoring_issues ADD COLUMN source_files TEXT"
            )
            migrations_applied.append("source_files")

        if migrations_applied:
            conn.commit()
            logger.info(
                f"Migrated self_monitoring_issues schema: added {migrations_applied}"
            )

    def _migrate_global_repos_schema(self, conn: sqlite3.Connection) -> None:
        """
        Migrate global_repos table schema for existing databases.

        Adds columns that were added after the initial table creation:
        - enable_scip: Whether SCIP code intelligence indexing is enabled

        This is safe to run multiple times - it only adds missing columns.
        """
        # Get existing columns
        cursor = conn.execute("PRAGMA table_info(global_repos)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        migrations_applied = []

        # Add missing columns
        if "enable_scip" not in existing_columns:
            conn.execute(
                "ALTER TABLE global_repos ADD COLUMN enable_scip BOOLEAN DEFAULT FALSE"
            )
            migrations_applied.append("enable_scip")

        if migrations_applied:
            conn.commit()
            logger.info(
                f"Migrated global_repos schema: added {migrations_applied}"
            )

    def _migrate_research_sessions_schema(self, conn: sqlite3.Connection) -> None:
        """
        Migrate research_sessions table schema for existing databases.

        Adds columns that were added after the initial table creation:
        - claude_session_id: UUID for Claude CLI session continuity (Bug fix for session resume)

        This is safe to run multiple times - it only adds missing columns.
        """
        # Get existing columns
        cursor = conn.execute("PRAGMA table_info(research_sessions)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        migrations_applied = []

        # Add missing columns
        if "claude_session_id" not in existing_columns:
            conn.execute(
                "ALTER TABLE research_sessions ADD COLUMN claude_session_id TEXT"
            )
            migrations_applied.append("claude_session_id")

        if migrations_applied:
            conn.commit()
            logger.info(
                f"Migrated research_sessions schema: added {migrations_applied}"
            )


class DatabaseConnectionManager:
    """
    Thread-local connection pooling with atomic transaction support.

    Each thread gets its own SQLite connection, enabling concurrent reads
    while maintaining proper isolation for writes.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize connection manager.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = db_path
        self._local = threading.local()
        self._connections: Dict[int, sqlite3.Connection] = {}
        self._lock = threading.Lock()

    def get_connection(self) -> sqlite3.Connection:
        """
        Get thread-local database connection.

        Returns the same connection for repeated calls from the same thread.
        Creates a new connection if one doesn't exist for the current thread.

        Returns:
            SQLite connection for the current thread.
        """
        thread_id = threading.get_ident()

        # Check if connection exists for this thread
        if not hasattr(self._local, "connection") or self._local.connection is None:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.connection = conn

            # Track connection for cleanup
            with self._lock:
                self._connections[thread_id] = conn

        connection: sqlite3.Connection = self._local.connection
        return connection

    def execute_atomic(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        """
        Execute operation atomically with exclusive transaction.

        Uses BEGIN EXCLUSIVE to prevent concurrent writes, ensuring data
        integrity. Commits on success, rolls back on any exception.

        Args:
            operation: Callable that takes a connection and performs database
                      operations. Return value is passed through.

        Returns:
            The return value from the operation callable.

        Raises:
            Any exception raised by the operation (after rollback).
        """
        conn = self.get_connection()
        conn.execute("BEGIN EXCLUSIVE")
        try:
            result = operation(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    def close_all(self) -> None:
        """
        Close all thread-local connections.

        Should be called during application shutdown to release resources.
        """
        with self._lock:
            for conn in self._connections.values():
                try:
                    conn.close()
                except Exception:
                    pass  # Ignore errors during cleanup
            self._connections.clear()

        # Clear local connection reference
        if hasattr(self._local, "connection"):
            self._local.connection = None
