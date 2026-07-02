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
import time
from pathlib import Path
from typing import Callable, Dict, Optional, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


# Bug #878 Fix A.2: Named operational constants for the wall-clock cleanup
# daemon. Centralised here so they can be overridden by future config wiring
# and kept consistent with the historical CLEANUP_INTERVAL (60 s).
#
# DEFAULT_CLEANUP_INTERVAL_SECONDS: sweep cadence for the daemon. Matches the
# legacy DatabaseConnectionManager.CLEANUP_INTERVAL so production behaviour
# is unchanged when no interval is passed explicitly.
DEFAULT_CLEANUP_INTERVAL_SECONDS: float = 60.0

# DEFAULT_CLEANUP_STOP_TIMEOUT_SECONDS: maximum time stop_cleanup_daemon()
# will wait for the daemon thread to exit.  2 s is comfortably larger than
# one Event.wait() tick yet short enough to keep shutdown snappy.
DEFAULT_CLEANUP_STOP_TIMEOUT_SECONDS: float = 2.0


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
            enable_scip BOOLEAN DEFAULT FALSE,
            next_refresh TEXT
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

    # Story #719: Hide Repositories from Auto-Discovery View
    CREATE_HIDDEN_DISCOVERY_REPOS_TABLE = """
        CREATE TABLE IF NOT EXISTS hidden_discovery_repos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_identifier TEXT NOT NULL UNIQUE,
            hidden_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            temporal_options TEXT,
            wiki_enabled INTEGER DEFAULT 0
        )
    """

    # Background Jobs table (Bug fix: BackgroundJobManager SQLite migration)
    # Story #876: executing_node and claimed_at added to match PostgreSQL
    # migration 001 (001_initial_schema.sql lines 150-151).  Both columns are
    # nullable with no default so existing rows remain valid after migration.
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
            language_resolution_status TEXT,
            executing_node TEXT,
            claimed_at TEXT
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

    # Story #269: Justified performance indexes (7 total)
    # background_jobs: queried by status and time ranges in dashboard/job-listing queries
    CREATE_IDX_BACKGROUND_JOBS_STATUS = """
        CREATE INDEX IF NOT EXISTS idx_background_jobs_status
        ON background_jobs(status)
    """

    CREATE_IDX_BACKGROUND_JOBS_STATUS_CREATED = """
        CREATE INDEX IF NOT EXISTS idx_background_jobs_status_created
        ON background_jobs(status, created_at DESC)
    """

    CREATE_IDX_BACKGROUND_JOBS_COMPLETED_STATUS = """
        CREATE INDEX IF NOT EXISTS idx_background_jobs_completed_status
        ON background_jobs(completed_at, status)
    """

    # Story #876 Phase C: cluster-atomic gate for register_job_if_no_conflict.
    # Mirrors PostgreSQL migration 004 — prevents two nodes from simultaneously
    # running the same (operation_type, repo_alias) job.  The partial predicate
    # limits uniqueness to ACTIVE jobs so completed/failed history can contain
    # duplicates (the same repo is refreshed many times over its lifetime).
    # SQLite supports partial indexes since 3.8.0 (2013).
    CREATE_IDX_ACTIVE_JOB_PER_REPO = """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
        ON background_jobs(operation_type, repo_alias)
        WHERE status IN ('pending', 'running')
          AND repo_alias IS NOT NULL
    """

    # Story #876: mirrors PostgreSQL migration 005 (005_executing_node_index.sql).
    # Indexed for cluster-mode queries that filter by executing_node to find
    # jobs owned by a specific node (e.g. DistributedJobClaimer.get_node_jobs).
    CREATE_IDX_BACKGROUND_JOBS_EXECUTING_NODE = """
        CREATE INDEX IF NOT EXISTS idx_background_jobs_executing_node
        ON background_jobs(executing_node)
        WHERE executing_node IS NOT NULL
    """

    # user_api_keys: looked up by username for user-specific key listing
    CREATE_IDX_USER_API_KEYS_USERNAME = """
        CREATE INDEX IF NOT EXISTS idx_user_api_keys_username
        ON user_api_keys(username)
    """

    # user_mcp_credentials: looked up by username (listing) and client_id (auth)
    CREATE_IDX_USER_MCP_CREDENTIALS_USERNAME = """
        CREATE INDEX IF NOT EXISTS idx_user_mcp_credentials_username
        ON user_mcp_credentials(username)
    """

    CREATE_IDX_USER_MCP_CREDENTIALS_CLIENT_ID = """
        CREATE INDEX IF NOT EXISTS idx_user_mcp_credentials_client_id
        ON user_mcp_credentials(client_id)
    """

    # research_messages: looked up by session_id to fetch conversation history
    # Single-column only — created_at ordering is done in Python, not SQL
    CREATE_IDX_RESEARCH_MESSAGES_SESSION_ID = """
        CREATE INDEX IF NOT EXISTS idx_research_messages_session_id
        ON research_messages(session_id)
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

    # Story #180: Repository Categories table
    CREATE_REPO_CATEGORIES_TABLE = """
        CREATE TABLE IF NOT EXISTS repo_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            pattern TEXT NOT NULL,
            priority INTEGER NOT NULL,
            created_at TEXT,
            updated_at TEXT
        )
    """

    # Story #190: Description Refresh Tracking table
    CREATE_DESCRIPTION_REFRESH_TRACKING_TABLE = """
        CREATE TABLE IF NOT EXISTS description_refresh_tracking (
            repo_alias TEXT PRIMARY KEY,
            last_run TEXT,
            next_run TEXT,
            status TEXT DEFAULT 'pending',
            error TEXT,
            last_known_commit TEXT,
            last_known_files_processed INTEGER,
            last_known_indexed_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """

    # Story #192: Dependency Map Tracking table (singleton)
    CREATE_DEPENDENCY_MAP_TRACKING_TABLE = """
        CREATE TABLE IF NOT EXISTS dependency_map_tracking (
            id INTEGER PRIMARY KEY,
            last_run TEXT,
            next_run TEXT,
            status TEXT DEFAULT 'pending',
            commit_hashes TEXT,
            error_message TEXT
        )
    """

    # Story #283: Wiki render cache tables
    CREATE_WIKI_CACHE_TABLE = """
        CREATE TABLE IF NOT EXISTS wiki_cache (
            repo_alias TEXT NOT NULL,
            article_path TEXT NOT NULL,
            rendered_html TEXT NOT NULL,
            title TEXT NOT NULL,
            file_mtime REAL NOT NULL,
            file_size INTEGER NOT NULL,
            rendered_at TEXT NOT NULL,
            PRIMARY KEY (repo_alias, article_path)
        )
    """

    CREATE_WIKI_SIDEBAR_CACHE_TABLE = """
        CREATE TABLE IF NOT EXISTS wiki_sidebar_cache (
            repo_alias TEXT PRIMARY KEY,
            sidebar_json TEXT NOT NULL,
            max_mtime REAL NOT NULL,
            built_at TEXT NOT NULL
        )
    """

    # Story #386: Git Credential Management with Identity Discovery
    CREATE_USER_GIT_CREDENTIALS_TABLE = """
        CREATE TABLE IF NOT EXISTS user_git_credentials (
            credential_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            forge_type TEXT NOT NULL,
            forge_host TEXT NOT NULL,
            encrypted_token TEXT NOT NULL,
            git_user_name TEXT,
            git_user_email TEXT,
            forge_username TEXT,
            name TEXT,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            UNIQUE(username, forge_type, forge_host)
        )
    """

    # Story #492: Cluster-Aware Dashboard - Node Metrics table
    CREATE_NODE_METRICS_TABLE = """
        CREATE TABLE IF NOT EXISTS node_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            node_ip TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            cpu_usage REAL NOT NULL DEFAULT 0.0,
            memory_percent REAL NOT NULL DEFAULT 0.0,
            memory_used_bytes INTEGER NOT NULL DEFAULT 0,
            process_rss_mb REAL NOT NULL DEFAULT 0.0,
            index_memory_mb REAL NOT NULL DEFAULT 0.0,
            swap_used_mb REAL NOT NULL DEFAULT 0.0,
            swap_total_mb REAL NOT NULL DEFAULT 0.0,
            disk_read_kb_s REAL NOT NULL DEFAULT 0.0,
            disk_write_kb_s REAL NOT NULL DEFAULT 0.0,
            net_rx_kb_s REAL NOT NULL DEFAULT 0.0,
            net_tx_kb_s REAL NOT NULL DEFAULT 0.0,
            volumes_json TEXT NOT NULL DEFAULT '[]',
            server_version TEXT NOT NULL DEFAULT ''
        )
    """

    CREATE_IDX_NODE_METRICS_NODE_TIMESTAMP = """
        CREATE INDEX IF NOT EXISTS idx_node_metrics_node_timestamp
        ON node_metrics(node_id, timestamp DESC)
    """

    CREATE_IDX_NODE_METRICS_TIMESTAMP = """
        CREATE INDEX IF NOT EXISTS idx_node_metrics_timestamp
        ON node_metrics(timestamp)
    """

    # Story #578: Centralized runtime configuration
    CREATE_SERVER_CONFIG_TABLE = """
        CREATE TABLE IF NOT EXISTS server_config (
            config_key TEXT PRIMARY KEY DEFAULT 'runtime',
            config_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT DEFAULT (datetime('now')),
            updated_by TEXT
        )
    """

    # Bug #587: Activated repo metadata for cluster mode
    CREATE_ACTIVATED_REPOS_TABLE = """
        CREATE TABLE IF NOT EXISTS activated_repos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            user_alias TEXT NOT NULL,
            golden_repo_alias TEXT,
            repo_path TEXT NOT NULL,
            current_branch TEXT DEFAULT 'main',
            activated_at TEXT,
            last_accessed TEXT,
            git_committer_email TEXT,
            ssh_key_used TEXT DEFAULT NULL,
            is_composite INTEGER DEFAULT 0,
            wiki_enabled INTEGER DEFAULT 0,
            metadata_json TEXT,
            UNIQUE(username, user_alias)
        )
    """

    CREATE_IDX_ACTIVATED_REPOS_USERNAME = """
        CREATE INDEX IF NOT EXISTS idx_activated_repos_username
        ON activated_repos(username)
    """

    CREATE_IDX_ACTIVATED_REPOS_GOLDEN = """
        CREATE INDEX IF NOT EXISTS idx_activated_repos_golden
        ON activated_repos(golden_repo_alias)
    """

    # Bug #573/#574: Generic rate limiting tables for cluster mode
    CREATE_RATE_LIMIT_FAILURES_TABLE = """
        CREATE TABLE IF NOT EXISTS rate_limit_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            limiter_type TEXT NOT NULL,
            identifier TEXT NOT NULL,
            failed_at REAL NOT NULL
        )
    """

    CREATE_IDX_RATE_LIMIT_FAILURES_LOOKUP = """
        CREATE INDEX IF NOT EXISTS idx_rate_limit_failures_lookup
        ON rate_limit_failures(limiter_type, identifier, failed_at)
    """

    CREATE_RATE_LIMIT_LOCKOUTS_TABLE = """
        CREATE TABLE IF NOT EXISTS rate_limit_lockouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            limiter_type TEXT NOT NULL,
            identifier TEXT NOT NULL,
            locked_until REAL NOT NULL,
            UNIQUE(limiter_type, identifier)
        )
    """

    # Bug #576: OIDC state tokens for cluster mode
    CREATE_OIDC_STATE_TOKENS_TABLE = """
        CREATE TABLE IF NOT EXISTS oidc_state_tokens (
            state_token TEXT PRIMARY KEY,
            state_data TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """

    # Bug #583: Token blacklist for cluster-wide JWT revocation
    CREATE_TOKEN_BLACKLIST_TABLE = """
        CREATE TABLE IF NOT EXISTS token_blacklist (
            jti TEXT PRIMARY KEY,
            blacklisted_at REAL NOT NULL
        )
    """

    # Bug #577: Delegation job results for cross-node visibility
    CREATE_DELEGATION_JOB_RESULTS_TABLE = """
        CREATE TABLE IF NOT EXISTS delegation_job_results (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            output TEXT,
            exit_code INTEGER,
            error TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT
        )
    """

    # Story #680: External Dependency Latency Observability
    # Stores raw per-request latency samples for windowed percentile computation.
    CREATE_DEPENDENCY_LATENCY_SAMPLES_TABLE = """
        CREATE TABLE IF NOT EXISTS dependency_latency_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            dependency_name TEXT NOT NULL,
            timestamp REAL NOT NULL,
            latency_ms REAL NOT NULL,
            status_code INTEGER NOT NULL
        )
    """

    CREATE_IDX_DEPENDENCY_LATENCY_DEP_TIMESTAMP = """
        CREATE INDEX IF NOT EXISTS idx_dependency_latency_dep_timestamp
        ON dependency_latency_samples(dependency_name, timestamp)
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

            # WAL mode: concurrent reads during writes — required even in test mode
            # so that background job threads writing to SQLite do not block HTTP
            # request threads reading from the same database (fixes "database is
            # locked" 500 errors in Phase 4 E2E tests).
            # In test mode (CIDX_TEST_FAST_SQLITE=1), keep synchronous=OFF to skip
            # fsync overhead while still allowing concurrent access.
            conn.execute("PRAGMA journal_mode = WAL")
            if os.environ.get("CIDX_TEST_FAST_SQLITE") == "1":
                conn.execute("PRAGMA synchronous = OFF")

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
            # Story #180: Repository Categories
            conn.execute(self.CREATE_REPO_CATEGORIES_TABLE)
            # Story #190: Description Refresh Tracking
            conn.execute(self.CREATE_DESCRIPTION_REFRESH_TRACKING_TABLE)
            # Story #192: Dependency Map Tracking
            conn.execute(self.CREATE_DEPENDENCY_MAP_TRACKING_TABLE)
            # Story #283: Wiki render cache tables
            conn.execute(self.CREATE_WIKI_CACHE_TABLE)
            conn.execute(self.CREATE_WIKI_SIDEBAR_CACHE_TABLE)
            # Story #386: Git Credential Management
            conn.execute(self.CREATE_USER_GIT_CREDENTIALS_TABLE)
            # Story #492: Cluster-Aware Dashboard - Node Metrics
            conn.execute(self.CREATE_NODE_METRICS_TABLE)
            conn.execute(self.CREATE_IDX_NODE_METRICS_NODE_TIMESTAMP)
            conn.execute(self.CREATE_IDX_NODE_METRICS_TIMESTAMP)
            # Story #269: Justified performance indexes (created on fresh databases)
            conn.execute(self.CREATE_IDX_BACKGROUND_JOBS_STATUS)
            conn.execute(self.CREATE_IDX_BACKGROUND_JOBS_STATUS_CREATED)
            conn.execute(self.CREATE_IDX_BACKGROUND_JOBS_COMPLETED_STATUS)
            # Story #876 Phase C: partial unique index for cluster-atomic
            # job registration.  Mirrors PostgreSQL migration 004.
            conn.execute(self.CREATE_IDX_ACTIVE_JOB_PER_REPO)
            conn.execute(self.CREATE_IDX_USER_API_KEYS_USERNAME)
            conn.execute(self.CREATE_IDX_USER_MCP_CREDENTIALS_USERNAME)
            conn.execute(self.CREATE_IDX_USER_MCP_CREDENTIALS_CLIENT_ID)
            conn.execute(self.CREATE_IDX_RESEARCH_MESSAGES_SESSION_ID)
            # Story #578: Server config centralization
            conn.execute(self.CREATE_SERVER_CONFIG_TABLE)
            # Bug #587: Activated repos cluster metadata
            conn.execute(self.CREATE_ACTIVATED_REPOS_TABLE)
            conn.execute(self.CREATE_IDX_ACTIVATED_REPOS_USERNAME)
            conn.execute(self.CREATE_IDX_ACTIVATED_REPOS_GOLDEN)
            # Bug #573/#574: Generic rate limiting tables
            conn.execute(self.CREATE_RATE_LIMIT_FAILURES_TABLE)
            conn.execute(self.CREATE_IDX_RATE_LIMIT_FAILURES_LOOKUP)
            conn.execute(self.CREATE_RATE_LIMIT_LOCKOUTS_TABLE)
            # Bug #576: OIDC state tokens for cluster mode
            conn.execute(self.CREATE_OIDC_STATE_TOKENS_TABLE)
            # Bug #583: Token blacklist for cluster-wide JWT revocation
            conn.execute(self.CREATE_TOKEN_BLACKLIST_TABLE)
            # Bug #577: Delegation job results for cross-node visibility
            conn.execute(self.CREATE_DELEGATION_JOB_RESULTS_TABLE)
            # Story #680: External Dependency Latency Observability
            conn.execute(self.CREATE_DEPENDENCY_LATENCY_SAMPLES_TABLE)
            conn.execute(self.CREATE_IDX_DEPENDENCY_LATENCY_DEP_TIMESTAMP)
            # Story #719: Hide Repositories from Auto-Discovery View
            conn.execute(self.CREATE_HIDDEN_DISCOVERY_REPOS_TABLE)

            conn.commit()

            # Run schema migrations for existing databases
            self._migrate_self_monitoring_issues_schema(conn)
            self._migrate_global_repos_schema(conn)
            self._migrate_research_sessions_schema(conn)
            self._migrate_golden_repos_metadata_category(conn)
            # Story #269: Drop unjustified indexes BEFORE creating justified ones
            # so the old composite research_messages index is dropped first
            self._migrate_drop_unjustified_indexes(conn)
            self._migrate_performance_indexes(conn)
            # Story #280/#283: Wiki feature migrations
            self._migrate_golden_repos_metadata_wiki(conn)
            self._migrate_wiki_cache_tables(conn)
            # Epic #261: JobTracker schema additions
            self._migrate_background_jobs_job_tracker(conn)
            # Bug fix: Add current_phase and phase_detail columns for progress persistence
            self._migrate_background_jobs_phase_fields(conn)
            # Story #386: Git Credential Management
            self._migrate_user_git_credentials(conn)
            # Story #492: Cluster-Aware Dashboard - Node Metrics (migration for existing DBs)
            self._migrate_node_metrics_table(conn)
            # Story #565: Password expiry - add password_changed_at column
            self._migrate_users_password_changed_at(conn)
            # Story #578: Server config centralization (migration for existing DBs)
            self._migrate_server_config_table(conn)
            # Bug #573/#574: Rate limiting tables (migration for existing DBs)
            self._migrate_rate_limit_tables(conn)
            # Bug #576: OIDC state tokens (migration for existing DBs)
            self._migrate_oidc_state_tokens_table(conn)
            # Bug #583: Token blacklist (migration for existing DBs)
            self._migrate_token_blacklist_table(conn)
            # Story #728: lifecycle_schema_version column for backfill detection
            self._migrate_description_refresh_lifecycle_version(conn)
            # Story #876 Phase C: partial unique index for cluster-atomic
            # register_job_if_no_conflict (mirrors PostgreSQL migration 004).
            self._migrate_active_job_unique_index(conn)
            # Story #876: executing_node + claimed_at columns and index
            # (mirrors PostgreSQL migrations 001 lines 150-151 and 005).
            self._migrate_add_executing_node_claimed_at(conn)

            logger.info(f"Database initialized at {db_path}")

        finally:
            conn.close()

    def _migrate_background_jobs_phase_fields(self, conn: sqlite3.Connection) -> None:
        """
        Add current_phase and phase_detail columns to background_jobs table.

        These columns store real-time phase progress information (Story #480)
        so it is persisted to SQLite and readable after a job leaves memory.

        Idempotent: checks for existing columns before adding.
        """
        cursor = conn.execute("PRAGMA table_info(background_jobs)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        migrations_applied = []

        if "current_phase" not in existing_columns:
            conn.execute("ALTER TABLE background_jobs ADD COLUMN current_phase TEXT")
            migrations_applied.append("current_phase")

        if "phase_detail" not in existing_columns:
            conn.execute("ALTER TABLE background_jobs ADD COLUMN phase_detail TEXT")
            migrations_applied.append("phase_detail")

        if migrations_applied:
            conn.commit()
            logger.info(
                f"Migrated background_jobs schema for phase progress: added {migrations_applied}"
            )

    def _migrate_user_git_credentials(self, conn: sqlite3.Connection) -> None:
        """
        Add user_git_credentials table if it doesn't exist (Story #386).

        Idempotent: uses CREATE TABLE IF NOT EXISTS so safe to run on any database.
        """
        conn.execute(self.CREATE_USER_GIT_CREDENTIALS_TABLE)
        conn.commit()
        logger.debug("Ensured user_git_credentials table exists")

    def _migrate_node_metrics_table(self, conn: sqlite3.Connection) -> None:
        """
        Add node_metrics table and indexes if they don't exist (Story #492).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS
        so safe to run on any existing database.

        This migration ensures the cluster node metrics table is present for
        both new installations and upgrades from older versions.
        """
        conn.execute(self.CREATE_NODE_METRICS_TABLE)
        conn.execute(self.CREATE_IDX_NODE_METRICS_NODE_TIMESTAMP)
        conn.execute(self.CREATE_IDX_NODE_METRICS_TIMESTAMP)
        conn.commit()
        logger.debug("Ensured node_metrics table and indexes exist")

    def _migrate_users_password_changed_at(self, conn: sqlite3.Connection) -> None:
        """
        Add password_changed_at column to users table (Story #565).

        Tracks when the user last changed their password for expiry enforcement.
        Idempotent: checks for existing column before adding.
        """
        cursor = conn.execute("PRAGMA table_info(users)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if "password_changed_at" not in existing_columns:
            conn.execute("ALTER TABLE users ADD COLUMN password_changed_at TEXT")
            conn.commit()
            logger.info("Migrated users schema: added password_changed_at column")

    def _migrate_background_jobs_job_tracker(self, conn: sqlite3.Connection) -> None:
        """
        Migrate background_jobs table for JobTracker (Epic #261 Story 1A).

        Adds columns:
        - progress_info: Human-readable progress description
        - metadata: JSON metadata for operation-specific context

        Adds indexes:
        - idx_background_jobs_op_repo_status: Conflict detection
        - idx_background_jobs_user_created: Per-user job listing
        - idx_background_jobs_created: Unfiltered recent jobs query

        Safe to run multiple times - idempotent.
        """
        cursor = conn.execute("PRAGMA table_info(background_jobs)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        migrations_applied = []

        if "progress_info" not in existing_columns:
            conn.execute("ALTER TABLE background_jobs ADD COLUMN progress_info TEXT")
            migrations_applied.append("progress_info")

        if "metadata" not in existing_columns:
            conn.execute("ALTER TABLE background_jobs ADD COLUMN metadata TEXT")
            migrations_applied.append("metadata")

        # Indexes use CREATE INDEX IF NOT EXISTS - always safe to run
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_background_jobs_op_repo_status
               ON background_jobs(operation_type, repo_alias, status)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_background_jobs_user_created
               ON background_jobs(username, created_at DESC)"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_background_jobs_created
               ON background_jobs(created_at DESC)"""
        )

        if migrations_applied:
            conn.commit()
            logger.info(
                f"Migrated background_jobs schema for JobTracker: added {migrations_applied}"
            )

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

        # Story #284: next_refresh for back-propagating jitter scheduling
        if "next_refresh" not in existing_columns:
            conn.execute("ALTER TABLE global_repos ADD COLUMN next_refresh TEXT")
            migrations_applied.append("next_refresh")

        if migrations_applied:
            conn.commit()
            logger.info(f"Migrated global_repos schema: added {migrations_applied}")

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

    def _migrate_golden_repos_metadata_category(self, conn: sqlite3.Connection) -> None:
        """
        Migrate golden_repos_metadata table schema for repository categories (Story #180).

        Adds columns:
        - category_id: Foreign key to repo_categories table (ON DELETE SET NULL)
        - category_auto_assigned: Boolean flag indicating if category was auto-assigned

        This is safe to run multiple times - it only adds missing columns.
        """
        # Get existing columns
        cursor = conn.execute("PRAGMA table_info(golden_repos_metadata)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        migrations_applied = []

        # Add category_id with foreign key constraint
        if "category_id" not in existing_columns:
            conn.execute(
                "ALTER TABLE golden_repos_metadata ADD COLUMN category_id INTEGER REFERENCES repo_categories(id) ON DELETE SET NULL"
            )
            migrations_applied.append("category_id")

        # Add category_auto_assigned flag
        if "category_auto_assigned" not in existing_columns:
            conn.execute(
                "ALTER TABLE golden_repos_metadata ADD COLUMN category_auto_assigned INTEGER DEFAULT 0"
            )
            migrations_applied.append("category_auto_assigned")

        if migrations_applied:
            conn.commit()
            logger.info(
                f"Migrated golden_repos_metadata schema: added {migrations_applied}"
            )

    def _migrate_drop_unjustified_indexes(self, conn: sqlite3.Connection) -> None:
        """
        Drop unjustified performance indexes from existing databases (Story #269).

        These 6 indexes were added in commit 0d5af105 but are not backed by
        real SQL query patterns. Dropping them reduces write overhead without
        harming read performance.

        Also drops the old composite idx_research_messages_session_id
        (session_id, created_at) so it can be re-created as single-column.

        Safe to run multiple times — DROP INDEX IF EXISTS is a no-op when absent.
        """
        unjustified = [
            "idx_background_jobs_operation_type",
            "idx_sync_jobs_username_status",
            "idx_sync_jobs_status",
            "idx_sync_jobs_created_at",
            "idx_user_api_keys_key_hash",
            # Drop the old composite research_messages index before re-creating
            # it as a single-column index in _migrate_performance_indexes
            "idx_research_messages_session_id",
        ]
        dropped = []
        for idx_name in unjustified:
            conn.execute(f"DROP INDEX IF EXISTS {idx_name}")
            dropped.append(idx_name)

        conn.commit()
        logger.info(
            f"Story #269: Dropped {len(dropped)} unjustified/stale indexes: {dropped}"
        )

    def _migrate_performance_indexes(self, conn: sqlite3.Connection) -> None:
        """
        Create the 7 justified performance indexes for existing databases (Story #269).

        These are idempotent (CREATE INDEX IF NOT EXISTS), so re-running on
        a database that already has them is a no-op.

        Must run AFTER _migrate_drop_unjustified_indexes so the old composite
        idx_research_messages_session_id is gone before the single-column
        version is created.
        """
        conn.execute(self.CREATE_IDX_BACKGROUND_JOBS_STATUS)
        conn.execute(self.CREATE_IDX_BACKGROUND_JOBS_STATUS_CREATED)
        conn.execute(self.CREATE_IDX_BACKGROUND_JOBS_COMPLETED_STATUS)
        conn.execute(self.CREATE_IDX_USER_API_KEYS_USERNAME)
        conn.execute(self.CREATE_IDX_USER_MCP_CREDENTIALS_USERNAME)
        conn.execute(self.CREATE_IDX_USER_MCP_CREDENTIALS_CLIENT_ID)
        conn.execute(self.CREATE_IDX_RESEARCH_MESSAGES_SESSION_ID)

        conn.commit()
        logger.info("Story #269: Ensured 7 justified performance indexes are present")

    def _migrate_golden_repos_metadata_wiki(self, conn: sqlite3.Connection) -> None:
        """Migrate golden_repos_metadata for wiki feature (Story #280).

        Adds wiki_enabled column to golden_repos_metadata.
        Safe to run multiple times - only adds missing columns.
        """
        existing_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(golden_repos_metadata)"
            ).fetchall()
        }
        migrations_applied = []
        if "wiki_enabled" not in existing_columns:
            conn.execute(
                "ALTER TABLE golden_repos_metadata ADD COLUMN wiki_enabled INTEGER DEFAULT 0"
            )
            migrations_applied.append("wiki_enabled")
        if migrations_applied:
            conn.commit()
            logger.info(
                f"Wiki migration applied to golden_repos_metadata: {migrations_applied}"
            )

    def _migrate_wiki_cache_tables(self, conn: sqlite3.Connection) -> None:
        """Create wiki cache tables for existing databases (Story #283).

        Idempotent - uses CREATE TABLE IF NOT EXISTS.
        """
        conn.execute(self.CREATE_WIKI_CACHE_TABLE)
        conn.execute(self.CREATE_WIKI_SIDEBAR_CACHE_TABLE)
        conn.commit()

    def _migrate_server_config_table(self, conn: sqlite3.Connection) -> None:
        """Create server_config table for existing databases (Story #578).

        Idempotent - uses CREATE TABLE IF NOT EXISTS.
        """
        conn.execute(self.CREATE_SERVER_CONFIG_TABLE)
        conn.commit()
        logger.debug("Ensured server_config table exists")

    def _migrate_rate_limit_tables(self, conn: sqlite3.Connection) -> None:
        """Create rate limiting tables for existing databases (Bug #573/#574).

        Idempotent - uses CREATE TABLE IF NOT EXISTS.
        """
        conn.execute(self.CREATE_RATE_LIMIT_FAILURES_TABLE)
        conn.execute(self.CREATE_IDX_RATE_LIMIT_FAILURES_LOOKUP)
        conn.execute(self.CREATE_RATE_LIMIT_LOCKOUTS_TABLE)
        conn.commit()
        logger.debug("Ensured rate_limit tables exist")

    def _migrate_oidc_state_tokens_table(self, conn: sqlite3.Connection) -> None:
        """Create OIDC state tokens table for existing databases (Bug #576).

        Idempotent - uses CREATE TABLE IF NOT EXISTS.
        """
        conn.execute(self.CREATE_OIDC_STATE_TOKENS_TABLE)
        conn.commit()
        logger.debug("Ensured oidc_state_tokens table exists")

    def _migrate_token_blacklist_table(self, conn: sqlite3.Connection) -> None:
        """Create token_blacklist table for existing databases (Bug #583).

        Idempotent - uses CREATE TABLE IF NOT EXISTS.
        """
        conn.execute(self.CREATE_TOKEN_BLACKLIST_TABLE)
        conn.commit()
        logger.debug("Ensured token_blacklist table exists")

    def _migrate_active_job_unique_index(self, conn: sqlite3.Connection) -> None:
        """Create partial unique index idx_active_job_per_repo (Story #876 Phase C).

        Mirrors PostgreSQL migration 004: prevents two cluster nodes from
        simultaneously registering the same (operation_type, repo_alias)
        active job.  The partial predicate limits uniqueness to pending/running
        jobs with a non-NULL repo_alias so historical duplicates and
        system-wide jobs remain allowed.

        Idempotent via CREATE UNIQUE INDEX IF NOT EXISTS.  Safe to run on any
        database regardless of age.
        """
        conn.execute(self.CREATE_IDX_ACTIVE_JOB_PER_REPO)
        conn.commit()
        logger.debug("Ensured idx_active_job_per_repo partial unique index exists")

    def _migrate_description_refresh_lifecycle_version(
        self, conn: sqlite3.Connection
    ) -> None:
        """Add lifecycle_schema_version column to description_refresh_tracking (Story #728).

        Tracks which lifecycle metadata schema version each repo's .md file was generated
        with.  Used by the backfill scheduler to detect repos that need re-generation.

        Idempotent: checks PRAGMA table_info before issuing ALTER TABLE so re-running
        on a database that already has the column is a no-op.
        """
        cursor = conn.execute("PRAGMA table_info(description_refresh_tracking)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if "lifecycle_schema_version" not in existing_columns:
            conn.execute(
                "ALTER TABLE description_refresh_tracking "
                "ADD COLUMN lifecycle_schema_version INTEGER DEFAULT 0"
            )
            conn.commit()
            logger.info(
                "Migrated description_refresh_tracking schema: "
                "added lifecycle_schema_version column (Story #728)"
            )

    def _migrate_add_executing_node_claimed_at(self, conn: sqlite3.Connection) -> None:
        """Add executing_node, claimed_at columns and executing_node index (Story #876).

        Closes the SQLite symmetry gap with PostgreSQL:
          - migration 001 (lines 150-151): executing_node TEXT, claimed_at TEXT
          - migration 005: CREATE INDEX ON background_jobs(executing_node)

        Both columns are nullable with no DEFAULT so existing rows remain
        valid — backward-compatible for rolling cluster upgrades.  The index
        creation uses CREATE INDEX IF NOT EXISTS for the same reason.

        Idempotent: checks PRAGMA table_info before each ALTER TABLE; the
        index DDL uses IF NOT EXISTS.  Re-running on an already-migrated
        database is a safe no-op.
        """
        cursor = conn.execute("PRAGMA table_info(background_jobs)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if "executing_node" not in existing_columns:
            conn.execute("ALTER TABLE background_jobs ADD COLUMN executing_node TEXT")
            logger.info(
                "Migrated background_jobs schema: "
                "added executing_node column (Story #876)"
            )

        if "claimed_at" not in existing_columns:
            conn.execute("ALTER TABLE background_jobs ADD COLUMN claimed_at TEXT")
            logger.info(
                "Migrated background_jobs schema: added claimed_at column (Story #876)"
            )

        conn.execute(self.CREATE_IDX_BACKGROUND_JOBS_EXECUTING_NODE)
        conn.commit()


class DatabaseConnectionManager:
    """
    Thread-local connection pooling with atomic transaction support.

    Each thread gets its own SQLite connection, enabling concurrent reads
    while maintaining proper isolation for writes.

    Bug #378: Use get_instance(db_path) to obtain a shared instance for a
    given database path. This singleton-per-path pattern prevents FD
    accumulation when multiple backend classes all open the same db file.
    """

    # Singleton registry: absolute db_path -> instance
    _instances: Dict[str, "DatabaseConnectionManager"] = {}
    _instance_lock: threading.Lock = threading.Lock()

    # Bug #517: Global cleanup state so that any instance's get_connection() call
    # triggers cleanup across ALL instances, not just the one being accessed.
    _last_global_cleanup: float = 0.0
    _global_cleanup_lock: threading.Lock = threading.Lock()

    # Bug #878 Fix A.2: Dedicated wall-clock cleanup daemon state.  Piggybacked
    # cleanup on get_connection() traffic was demand-driven and lost races to
    # short-lived thread churn (observed cleanup gaps of 1-16 minutes in
    # production).  A background daemon thread wakes on a fixed wall-clock
    # cadence and sweeps every registered instance regardless of traffic.
    _cleanup_thread: Optional[threading.Thread] = None
    _cleanup_stop_event: Optional[threading.Event] = None
    _cleanup_daemon_lock: threading.Lock = threading.Lock()

    @classmethod
    def _cleanup_all_instances(cls) -> None:
        """
        Clean stale connections across ALL singleton instances (throttled).

        Bug #517: When only per-instance cleanup existed, infrequently-accessed
        instances never had their dead-thread connections removed.  Every
        registered instance is swept, bounding FD growth regardless of access
        pattern.

        Throttled by _last_global_cleanup / _global_cleanup_lock at the class
        level so the sweep runs at most once per CLEANUP_INTERVAL across the
        entire process.  Retained for backward compatibility with historical
        callers (e.g. tests asserting Bug #517 throttle semantics).  The
        dedicated cleanup daemon (Bug #878 Fix A.2) calls
        _sweep_all_instances_unthrottled directly instead, because the daemon's
        wall-clock interval already provides throttling.
        """
        with cls._global_cleanup_lock:
            now = time.time()
            # Re-check inside the lock (double-checked locking pattern)
            # CLEANUP_INTERVAL is an instance attribute; use a sentinel default
            interval = next(
                (inst.CLEANUP_INTERVAL for inst in cls._instances.values()),
                DEFAULT_CLEANUP_INTERVAL_SECONDS,
            )
            if (now - cls._last_global_cleanup) <= interval:
                return
            cls._last_global_cleanup = now
            instances_snapshot = list(cls._instances.values())

        for inst in instances_snapshot:
            inst._cleanup_stale_connections()

    @classmethod
    def _sweep_all_instances_unthrottled(cls) -> None:
        """
        Unthrottled sweep across ALL singleton instances.

        Bug #878 Fix A.2: The cleanup daemon calls this on every tick because
        the daemon's wall-clock interval IS the throttle.  We still update
        _last_global_cleanup for telemetry continuity so dashboards and log
        audits that watch that timestamp keep working.
        """
        with cls._global_cleanup_lock:
            cls._last_global_cleanup = time.time()
            instances_snapshot = list(cls._instances.values())

        for inst in instances_snapshot:
            inst._cleanup_stale_connections()

    @classmethod
    def start_cleanup_daemon(
        cls, interval: float = DEFAULT_CLEANUP_INTERVAL_SECONDS
    ) -> None:
        """
        Start the wall-clock cleanup daemon. Idempotent.

        Bug #878 Fix A.2: Spawns a single background thread that wakes every
        `interval` seconds and invokes cls._cleanup_all_instances(), sweeping
        every registered DatabaseConnectionManager regardless of whether any
        thread called get_connection() recently.  This replaces the
        demand-driven piggyback cleanup that lost races to short-lived
        BackgroundJob thread churn.

        If a daemon is already running, this is a no-op: the existing thread
        keeps its prior interval.  Use stop_cleanup_daemon() +
        start_cleanup_daemon() to change cadence.

        Args:
            interval: Seconds between sweeps.  Must be > 0.  Defaults to
                DEFAULT_CLEANUP_INTERVAL_SECONDS (60 s), matching the
                historical CLEANUP_INTERVAL.

        Raises:
            ValueError: If `interval` is not strictly positive.
        """
        if not interval > 0:
            raise ValueError(
                f"start_cleanup_daemon: interval must be > 0, got {interval}"
            )
        with cls._cleanup_daemon_lock:
            if cls._cleanup_thread is not None and cls._cleanup_thread.is_alive():
                return
            cls._cleanup_stop_event = threading.Event()
            cls._cleanup_thread = threading.Thread(
                target=cls._cleanup_daemon_loop,
                args=(interval,),
                name="DatabaseConnectionManager-cleanup-daemon",
                daemon=True,
            )
            cls._cleanup_thread.start()
            logger.info(
                "DatabaseConnectionManager cleanup daemon started (interval=%ss)",
                interval,
            )

    @classmethod
    def stop_cleanup_daemon(
        cls, timeout: float = DEFAULT_CLEANUP_STOP_TIMEOUT_SECONDS
    ) -> None:
        """
        Stop the cleanup daemon cleanly.

        Bug #878 Fix A.2: Signals the daemon's stop Event, joins with a bounded
        timeout, and -- only if the thread has actually exited AND the class
        references still point to the thread we were asked to stop -- clears
        those references so a subsequent start_cleanup_daemon() call can spawn
        a fresh daemon.

        The identity check (`cls._cleanup_thread is thread`) is required
        because, while this method is blocked in `thread.join()`, another
        caller may observe the old thread exit, invoke start_cleanup_daemon(),
        and install a brand-new thread/event pair.  Blindly clearing the
        class references after the outer join would then null out the newer
        daemon's bookkeeping while the newer daemon is still running,
        breaking the single-daemon invariant.

        If join() times out (thread still alive), the class references are
        preserved so a later start call remains a no-op rather than spawning
        a second daemon alongside the stuck one.

        Safe to call when no daemon is running (no-op).

        Args:
            timeout: Max seconds to wait for the daemon thread to exit.  Must
                be >= 0.  Defaults to DEFAULT_CLEANUP_STOP_TIMEOUT_SECONDS (2 s).

        Raises:
            ValueError: If `timeout` is negative.
        """
        if timeout < 0:
            raise ValueError(
                f"stop_cleanup_daemon: timeout must be >= 0, got {timeout}"
            )
        with cls._cleanup_daemon_lock:
            thread = cls._cleanup_thread
            stop = cls._cleanup_stop_event
        if thread is None or stop is None:
            return
        stop.set()
        thread.join(timeout=timeout)
        if thread.is_alive():
            # Join timed out.  Do NOT clear class references -- keeping the
            # live thread recorded preserves the single-daemon invariant so a
            # later start_cleanup_daemon() call remains a no-op instead of
            # spawning a second daemon alongside this stuck one.
            logger.warning(
                "DatabaseConnectionManager cleanup daemon did not exit within "
                "%ss; keeping thread reference to preserve single-daemon "
                "invariant",
                timeout,
            )
            return
        with cls._cleanup_daemon_lock:
            # Identity-guarded clear: only null the class references if they
            # still point to the thread/event pair we just joined.  A
            # concurrent start_cleanup_daemon() between our thread.join()
            # returning and reacquiring this lock may have already installed
            # a newer daemon.
            if cls._cleanup_thread is thread and cls._cleanup_stop_event is stop:
                cls._cleanup_thread = None
                cls._cleanup_stop_event = None
                logger.info("DatabaseConnectionManager cleanup daemon stopped")
            else:
                logger.info(
                    "DatabaseConnectionManager cleanup daemon stopped; "
                    "a newer daemon is already running (class references "
                    "preserved)"
                )

    @classmethod
    def _cleanup_daemon_loop(cls, interval: float) -> None:
        """
        Daemon loop body.

        Wakes every `interval` seconds (or earlier if the stop event is set)
        and invokes cls._sweep_all_instances_unthrottled().  The daemon's
        own wall-clock interval IS the throttle, so bypassing the
        60-second Bug #517 throttle on _cleanup_all_instances is
        intentional.  Any exception raised by the sweep is logged and
        swallowed so a single bad iteration cannot kill the daemon --
        the next tick will retry.
        """
        stop = cls._cleanup_stop_event
        assert stop is not None, "cleanup daemon started without stop event"
        while not stop.wait(interval):
            try:
                cls._sweep_all_instances_unthrottled()
            except Exception as exc:
                logger.warning("Cleanup daemon sweep raised, continuing: %s", exc)

    @classmethod
    def get_instance(cls, db_path: str) -> "DatabaseConnectionManager":
        """
        Get the shared DatabaseConnectionManager instance for a given db_path.

        Uses double-checked locking to ensure thread-safe singleton creation.
        Normalises the path via os.path.abspath so that relative and absolute
        paths pointing to the same file share a single instance.

        Args:
            db_path: Path to the SQLite database file.

        Returns:
            The shared DatabaseConnectionManager instance for that file.
        """
        resolved = os.path.abspath(db_path)
        if resolved not in cls._instances:
            with cls._instance_lock:
                if resolved not in cls._instances:
                    cls._instances[resolved] = cls(db_path)
        return cls._instances[resolved]

    def __init__(self, db_path: str) -> None:
        """
        Initialize connection manager.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = db_path
        self._local = threading.local()
        self._connections: Dict[int, sqlite3.Connection] = {}
        # RLock (re-entrant) is required: _cleanup_stale_connections() holds
        # self._lock while logging (logger.warning / logger.info at lines 1191,
        # 1197).  If SQLiteLogHandler is at the root logger, those log calls
        # re-enter emit() -> execute_atomic() -> get_connection(), which tries
        # `with self._lock:` on a thread that has no prior connection.  A plain
        # threading.Lock would deadlock (same thread, non-re-entrant).
        # RLock allows the same thread to re-acquire without blocking.
        # Bug #731 primary fix.
        self._lock = threading.RLock()
        self._last_cleanup: float = 0.0
        self.CLEANUP_INTERVAL: float = 60.0

    def _cleanup_stale_connections(self) -> None:
        """
        Close and remove connections for threads that are no longer alive.

        Called periodically (throttled by CLEANUP_INTERVAL) to prevent
        unbounded memory and file descriptor growth from thread pool churn.
        """
        with self._lock:
            self._last_cleanup = time.time()
            alive_thread_ids = {t.ident for t in threading.enumerate()}
            stale_ids = [
                tid for tid in self._connections if tid not in alive_thread_ids
            ]
            for tid in stale_ids:
                try:
                    self._connections[tid].close()
                except Exception as e:
                    logger.warning(
                        f"Failed to close stale connection for thread {tid}: {e}"
                    )
                del self._connections[tid]

            if stale_ids:
                logger.info(f"Cleaned up {len(stale_ids)} stale SQLite connections")

    def close_thread_connection(self) -> None:
        """
        Close and untrack the calling thread's connection.  Bug #878 Fix A.3.

        Called from BackgroundJobManager._execute_job's outer finally block
        to proactively release SQLite file descriptors at thread exit
        instead of relying on the wall-clock cleanup daemon
        (Bug #878 Fix A.2) to discover the TID as stale some seconds
        later.  Under heavy job-thread churn the daemon cannot keep up
        (see Root Cause RC-3), so closing at the source of the churn
        eliminates the race entirely.

        Semantics:
          - Pops any tracked connection for the current TID out of
            self._connections (under self._lock).
          - Closes both the tracked connection and the thread-local
            connection reference, if they differ.  Double-close is
            harmless on sqlite3.Connection, but we guard against it
            anyway by only closing each object once.
          - Resets self._local.connection to None so a subsequent
            get_connection() on this same thread would open a fresh
            connection (rather than reuse the just-closed one).
          - Swallows individual close() exceptions with a WARNING log
            so a single broken connection cannot leave state half-
            cleaned up.  The outer finally in _execute_job also
            catches exceptions raised by this whole method so one
            broken manager cannot stop cleanup on sibling managers.
        """
        tid = threading.get_ident()
        with self._lock:
            tracked = self._connections.pop(tid, None)
        local_conn = getattr(self._local, "connection", None)
        try:
            if local_conn is not None:
                try:
                    local_conn.close()
                except Exception as exc:
                    logger.warning(
                        "close_thread_connection: local close raised for tid=%s: %s",
                        tid,
                        exc,
                    )
            if tracked is not None and tracked is not local_conn:
                try:
                    tracked.close()
                except Exception as exc:
                    logger.warning(
                        "close_thread_connection: tracked close raised for tid=%s: %s",
                        tid,
                        exc,
                    )
        finally:
            self._local.connection = None

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
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            self._local.connection = conn

            # Track connection for cleanup.
            # Bug #878 Fix A.1: Close-on-clobber. When a Linux OS thread ID
            # (TID) is recycled, a new BackgroundJob thread may land on the
            # same TID as a previous dead thread. Its threading.local is
            # empty, so a fresh connection is opened, but the prior
            # connection remains tracked at this TID. Without the explicit
            # close below, that connection leaks silently until Python GC
            # collects it -- and GC timing is non-deterministic, so FDs can
            # accumulate faster than GC drains them.
            with self._lock:
                existing = self._connections.get(thread_id)
                if existing is not None and existing is not conn:
                    try:
                        existing.close()
                    except Exception as exc:
                        # Best-effort close: log and continue so the new
                        # connection still replaces the stale entry.
                        logger.warning(
                            "Failed to close clobbered SQLite connection "
                            "for thread %s: %s",
                            thread_id,
                            exc,
                        )
                self._connections[thread_id] = conn

        # Bug #878 Fix A.2: The piggyback cleanup check that used to fire
        # here has been removed.  Cleanup is now driven by the dedicated
        # wall-clock daemon started via DatabaseConnectionManager
        # .start_cleanup_daemon() during FastAPI lifespan startup.  That
        # daemon sweeps all registered instances on a fixed cadence
        # regardless of get_connection() traffic, eliminating the
        # demand-driven race (Root Cause RC-3) where short-lived thread
        # churn outpaced the piggyback sweep.  _last_global_cleanup is
        # still maintained by _cleanup_all_instances() for telemetry.

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
        Close all thread-local connections and deregister this instance.

        Should be called during application shutdown to release resources.
        Removes this instance from the singleton registry so that a subsequent
        get_instance() call for the same path creates a fresh instance with
        clean thread-local state.  Without deregistration, a stopped-and-
        restarted app (common in tests) retrieves the same stale instance whose
        connections are already closed, causing "Cannot operate on a closed
        database" errors.
        """
        with self._lock:
            for conn in self._connections.values():
                try:
                    conn.close()
                except Exception as exc:
                    logger.warning(
                        "Failed to close SQLite connection during shutdown: %s", exc
                    )
            self._connections.clear()

        # Clear local connection reference
        if hasattr(self._local, "connection"):
            self._local.connection = None

        # Deregister from the singleton registry so the next get_instance()
        # call for this path creates a fresh instance.
        resolved = os.path.abspath(self.db_path)
        with self.__class__._instance_lock:
            self.__class__._instances.pop(resolved, None)
