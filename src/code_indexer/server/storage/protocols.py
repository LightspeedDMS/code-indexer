"""
Python Protocol interfaces for all storage backends (Story #410).

Defines PEP 544 structural subtyping Protocols for each SQLite backend,
allowing PostgreSQL (or any other) implementations to be drop-in replacements
without inheriting from a common base class.

All Protocols are decorated with @runtime_checkable so isinstance() checks
work in tests and at runtime.

Usage:
    from code_indexer.server.storage.protocols import GlobalReposBackend

    def use_backend(backend: GlobalReposBackend) -> None:
        ...
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# GlobalReposBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class GlobalReposBackend(Protocol):
    """Protocol for global repository registry storage."""

    def register_repo(
        self,
        alias_name: str,
        repo_name: str,
        repo_url: Optional[str],
        index_path: str,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict[str, Any]] = None,
        enable_scip: bool = False,
    ) -> None: ...

    def get_repo(self, alias_name: str) -> Optional[Dict[str, Any]]: ...

    def list_repos(self) -> Dict[str, Dict[str, Any]]: ...

    def delete_repo(self, alias_name: str) -> bool: ...

    def update_last_refresh(self, alias_name: str) -> bool: ...

    def update_enable_temporal(
        self, alias_name: str, enable_temporal: bool
    ) -> bool: ...

    def update_enable_scip(self, alias_name: str, enable_scip: bool) -> bool: ...

    def update_next_refresh(
        self, alias_name: str, next_refresh: Optional[str]
    ) -> bool: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# UsersBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class UsersBackend(Protocol):
    """Protocol for user management storage."""

    def create_user(
        self,
        username: str,
        password_hash: str,
        role: str,
        email: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None: ...

    def get_user(self, username: str) -> Optional[Dict[str, Any]]: ...

    def list_users(self) -> list: ...

    def update_user(
        self,
        username: str,
        new_username: Optional[str] = None,
        email: Optional[str] = None,
    ) -> bool: ...

    def delete_user(self, username: str) -> bool: ...

    def update_user_role(self, username: str, role: str) -> bool: ...

    def update_password_hash(self, username: str, password_hash: str) -> bool: ...

    def add_api_key(
        self,
        username: str,
        key_id: str,
        key_hash: str,
        key_prefix: str,
        name: Optional[str] = None,
    ) -> None: ...

    def delete_api_key(self, username: str, key_id: str) -> bool: ...

    def add_mcp_credential(
        self,
        username: str,
        credential_id: str,
        client_id: str,
        client_secret_hash: str,
        client_id_prefix: str,
        name: Optional[str] = None,
    ) -> None: ...

    def delete_mcp_credential(self, username: str, credential_id: str) -> bool: ...

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]: ...

    def set_oidc_identity(self, username: str, identity: Dict[str, Any]) -> bool: ...

    def remove_oidc_identity(self, username: str) -> bool: ...

    def update_mcp_credential_last_used(
        self, username: str, credential_id: str
    ) -> bool: ...

    def list_all_mcp_credentials(
        self, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]: ...

    def get_system_mcp_credentials(self) -> List[Dict[str, Any]]: ...

    def get_mcp_credential_by_client_id(
        self, client_id: str
    ) -> Optional[Tuple[str, dict]]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# SessionsBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionsBackend(Protocol):
    """Protocol for session management storage."""

    def invalidate_session(self, username: str, token_id: str) -> None: ...

    def is_session_invalidated(self, username: str, token_id: str) -> bool: ...

    def clear_invalidated_sessions(self, username: str) -> None: ...

    def set_password_change_timestamp(self, username: str, changed_at: str) -> None: ...

    def get_password_change_timestamp(self, username: str) -> Optional[str]: ...

    def cleanup_old_data(self, days_to_keep: int = 30) -> int: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# BackgroundJobsBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class BackgroundJobsBackend(Protocol):
    """Protocol for background job management storage."""

    def save_job(
        self,
        job_id: str,
        operation_type: str,
        status: str,
        created_at: str,
        username: str,
        progress: int,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        is_admin: bool = False,
        cancelled: bool = False,
        repo_alias: Optional[str] = None,
        resolution_attempts: int = 0,
        claude_actions: Optional[List[str]] = None,
        failure_reason: Optional[str] = None,
        extended_error: Optional[Dict[str, Any]] = None,
        language_resolution_status: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None: ...

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]: ...

    def update_job(self, job_id: str, **kwargs: Any) -> None: ...

    def list_jobs(
        self,
        username: Optional[str] = None,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]: ...

    def list_jobs_filtered(
        self,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        search_text: Optional[str] = None,
        exclude_ids: Optional[Any] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> tuple: ...

    def delete_job(self, job_id: str) -> bool: ...

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int: ...

    def count_jobs_by_status(self) -> Dict[str, int]: ...

    def get_job_stats(self, time_filter: str = "24h") -> Dict[str, int]: ...

    def cleanup_orphaned_jobs_on_startup(self) -> int: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# SyncJobsBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class SyncJobsBackend(Protocol):
    """Protocol for sync job management storage."""

    def create_job(
        self,
        job_id: str,
        username: str,
        user_alias: str,
        job_type: str,
        status: str,
        repository_url: Optional[str] = None,
    ) -> None: ...

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]: ...

    def update_job(self, job_id: str, **kwargs: Any) -> None: ...

    def list_jobs(self) -> list: ...

    def delete_job(self, job_id: str) -> bool: ...

    def cleanup_orphaned_jobs_on_startup(self) -> int: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# CITokensBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class CITokensBackend(Protocol):
    """Protocol for CI token storage."""

    def save_token(
        self, platform: str, encrypted_token: str, base_url: Optional[str] = None
    ) -> None: ...

    def get_token(self, platform: str) -> Optional[Dict[str, Any]]: ...

    def delete_token(self, platform: str) -> bool: ...

    def list_tokens(self) -> Dict[str, Dict[str, Any]]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# DescriptionRefreshTrackingBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class DescriptionRefreshTrackingBackend(Protocol):
    """Protocol for description refresh tracking storage."""

    def get_tracking_record(self, repo_alias: str) -> Optional[Dict[str, Any]]: ...

    def get_stale_repos(self, now_iso: str) -> List[Dict[str, Any]]: ...

    def upsert_tracking(self, repo_alias: str, **fields: Any) -> None: ...

    def delete_tracking(self, repo_alias: str) -> bool: ...

    def get_all_tracking(self) -> List[Dict[str, Any]]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# SSHKeysBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class SSHKeysBackend(Protocol):
    """Protocol for SSH key management storage."""

    def create_key(
        self,
        name: str,
        fingerprint: str,
        key_type: str,
        private_path: str,
        public_path: str,
        public_key: Optional[str] = None,
        email: Optional[str] = None,
        description: Optional[str] = None,
        is_imported: bool = False,
    ) -> None: ...

    def get_key(self, name: str) -> Optional[Dict[str, Any]]: ...

    def assign_host(self, key_name: str, hostname: str) -> None: ...

    def remove_host(self, key_name: str, hostname: str) -> None: ...

    def delete_key(self, name: str) -> bool: ...

    def list_keys(self) -> list: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# GoldenRepoMetadataBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class GoldenRepoMetadataBackend(Protocol):
    """Protocol for golden repository metadata storage."""

    def ensure_table_exists(self) -> None: ...

    def add_repo(
        self,
        alias: str,
        repo_url: str,
        default_branch: str,
        clone_path: str,
        created_at: str,
        enable_temporal: bool = False,
        temporal_options: Optional[Dict] = None,
    ) -> None: ...

    def get_repo(self, alias: str) -> Optional[Dict[str, Any]]: ...

    def list_repos(self) -> List[Dict[str, Any]]: ...

    def remove_repo(self, alias: str) -> bool: ...

    def repo_exists(self, alias: str) -> bool: ...

    def update_enable_temporal(self, alias: str, enable: bool) -> bool: ...

    def update_repo_url(self, alias: str, repo_url: str) -> bool: ...

    def update_category(
        self, alias: str, category_id: Optional[int], auto_assigned: bool = True
    ) -> bool: ...

    def update_wiki_enabled(self, alias: str, enabled: bool) -> None: ...

    def update_default_branch(self, alias: str, branch: str) -> None: ...

    def invalidate_description_refresh_tracking(self, alias: str) -> None: ...

    def invalidate_dependency_map_tracking(self, alias: str) -> None: ...

    def list_repos_with_categories(self) -> List[Dict[str, Any]]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# DependencyMapTrackingBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class DependencyMapTrackingBackend(Protocol):
    """Protocol for dependency map tracking storage."""

    def get_tracking(self) -> Dict[str, Any]: ...

    def update_tracking(
        self,
        last_run: Any = ...,
        next_run: Any = ...,
        status: Any = ...,
        commit_hashes: Any = ...,
        error_message: Any = ...,
        refinement_cursor: Any = ...,
        refinement_next_run: Any = ...,
    ) -> None: ...

    def cleanup_stale_status_on_startup(self) -> bool: ...

    def record_run_metrics(self, metrics: Dict[str, Any]) -> None: ...

    def get_run_history(self, limit: int = 5) -> List[Dict[str, Any]]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# GitCredentialsBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class GitCredentialsBackend(Protocol):
    """Protocol for user git credentials storage."""

    def upsert_credential(
        self,
        credential_id: str,
        username: str,
        forge_type: str,
        forge_host: str,
        encrypted_token: str,
        git_user_name: Optional[str] = None,
        git_user_email: Optional[str] = None,
        forge_username: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None: ...

    def list_credentials(self, username: str) -> List[Dict[str, Any]]: ...

    def delete_credential(self, username: str, credential_id: str) -> bool: ...

    def get_credential_for_host(
        self, username: str, forge_host: str
    ) -> Optional[Dict[str, Any]]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# RepoCategoryBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class RepoCategoryBackend(Protocol):
    """Protocol for repository category management storage."""

    def create_category(self, name: str, pattern: str, priority: int) -> int: ...

    def list_categories(self) -> List[Dict[str, Any]]: ...

    def get_category(self, category_id: int) -> Optional[Dict[str, Any]]: ...

    def update_category(self, category_id: int, name: str, pattern: str) -> None: ...

    def delete_category(self, category_id: int) -> None: ...

    def reorder_categories(self, ordered_ids: List[int]) -> None: ...

    def shift_all_priorities(self) -> None: ...

    def get_next_priority(self) -> int: ...

    def get_repo_category_map(self) -> Dict[str, Dict[str, Any]]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# GroupsBackend (GroupAccessManager storage interface)
# ---------------------------------------------------------------------------


@runtime_checkable
class GroupsBackend(Protocol):
    """Protocol for group access management storage (GroupAccessManager interface)."""

    def get_all_groups(self) -> list: ...

    def get_group(self, group_id: int) -> Optional[Any]: ...

    def get_group_by_name(self, name: str) -> Optional[Any]: ...

    def create_group(self, name: str, description: str) -> Any: ...

    def update_group(
        self,
        group_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[Any]: ...

    def delete_group(self, group_id: int) -> bool: ...

    def assign_user_to_group(
        self, user_id: str, group_id: int, assigned_by: str
    ) -> None: ...

    def remove_user_from_group(self, user_id: str, group_id: int) -> bool: ...

    def get_user_group(self, user_id: str) -> Optional[Any]: ...

    def get_user_membership(self, user_id: str) -> Optional[Any]: ...

    def get_users_in_group(self, group_id: int) -> List[str]: ...

    def get_user_count_in_group(self, group_id: int) -> int: ...

    def grant_repo_access(
        self, repo_name: str, group_id: int, granted_by: str
    ) -> bool: ...

    def revoke_repo_access(self, repo_name: str, group_id: int) -> bool: ...

    def get_group_repos(self, group_id: int) -> List[str]: ...

    def get_repo_groups(self, repo_name: str) -> list: ...

    def get_repo_access(self, repo_name: str, group_id: int) -> Optional[Any]: ...

    def auto_assign_golden_repo(self, repo_name: str) -> None: ...

    def get_all_users_with_groups(
        self, limit: Optional[int] = None, offset: int = 0
    ) -> tuple: ...

    def user_exists(self, user_id: str) -> bool: ...

    def log_audit(
        self,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
    ) -> None: ...

    def get_audit_logs(
        self,
        action_type: Optional[str] = None,
        target_type: Optional[str] = None,
        admin_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        exclude_target_type: Optional[str] = None,
    ) -> tuple: ...


# ---------------------------------------------------------------------------
# AuditLogBackend (AuditLogService storage interface)
# ---------------------------------------------------------------------------


@runtime_checkable
class AuditLogBackend(Protocol):
    """Protocol for audit log service storage (AuditLogService interface)."""

    def log(
        self,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
    ) -> None: ...

    def log_raw(
        self,
        timestamp: str,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
    ) -> None: ...

    def query(
        self,
        action_type: Optional[str] = None,
        target_type: Optional[str] = None,
        admin_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        exclude_target_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Tuple[List[dict], int]: ...

    def get_pr_logs(
        self,
        repo_alias: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]: ...

    def get_cleanup_logs(
        self,
        repo_path: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]: ...


# ---------------------------------------------------------------------------
# NodeMetricsBackend (Story #492: Cluster-Aware Dashboard)
# ---------------------------------------------------------------------------


@runtime_checkable
class NodeMetricsBackend(Protocol):
    """Protocol for cluster node metrics storage (Story #492).

    Supports both SQLite (standalone) and PostgreSQL (cluster) backends.
    Each node writes snapshots periodically; the dashboard reads the latest
    snapshot per node to render the cluster health carousel.
    """

    def write_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Write a single metrics snapshot for this node.

        Args:
            snapshot: Dict with keys: node_id, node_ip, timestamp, cpu_usage,
                memory_percent, memory_used_bytes, process_rss_mb, index_memory_mb,
                swap_used_mb, swap_total_mb, disk_read_kb_s, disk_write_kb_s,
                net_rx_kb_s, net_tx_kb_s, volumes_json, server_version.
        """
        ...

    def get_latest_per_node(self) -> List[Dict[str, Any]]:
        """Return the latest snapshot for each distinct node_id.

        Returns:
            List of snapshot dicts, one per distinct node_id, ordered by
            node_id. Each dict has all snapshot fields.
        """
        ...

    def get_all_snapshots(self, since: datetime) -> List[Dict[str, Any]]:
        """Return all snapshots since the given datetime.

        Args:
            since: Datetime cutoff; only snapshots with timestamp >= since are returned.

        Returns:
            List of snapshot dicts ordered by timestamp ascending.
        """
        ...

    def cleanup_older_than(self, cutoff: datetime) -> int:
        """Delete all snapshots with timestamp older than cutoff.

        Args:
            cutoff: Datetime threshold; records with timestamp < cutoff are deleted.

        Returns:
            Number of rows deleted.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...


# ---------------------------------------------------------------------------
# LogsBackend (Story #500: LogsBackend Protocol and SQLite Wrapper)
# ---------------------------------------------------------------------------


@runtime_checkable
class LogsBackend(Protocol):
    """Protocol for operational log storage (Story #500).

    Supports both SQLite (standalone) and PostgreSQL (cluster) backends.
    Each node writes log records; the admin UI and REST API read them back
    with filtering and pagination.
    """

    def insert_log(
        self,
        timestamp: str,
        level: str,
        source: str,
        message: str,
        correlation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        request_path: Optional[str] = None,
        extra_data: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> None:
        """Insert a single log record.

        Args:
            timestamp: ISO 8601 timestamp string.
            level: Log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            source: Logger name / source identifier.
            message: Formatted log message text.
            correlation_id: Optional request correlation ID.
            user_id: Optional user identifier.
            request_path: Optional HTTP request path.
            extra_data: Optional JSON-serialised extra fields.
            node_id: Optional cluster node identifier (NULL in standalone).
        """
        ...

    def query_logs(
        self,
        level: Optional[str] = None,
        source: Optional[str] = None,
        correlation_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        node_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> "Tuple[List[Dict], int]":
        """Query log records with optional filtering and pagination.

        Args:
            level: Filter by log level (optional).
            source: Filter by logger name (optional).
            correlation_id: Filter by correlation ID (optional).
            date_from: ISO 8601 lower bound for timestamp (inclusive, optional).
            date_to: ISO 8601 upper bound for timestamp (inclusive, optional).
            node_id: Filter by cluster node ID (optional).
            limit: Maximum number of records to return (default 100).
            offset: Number of records to skip for pagination (default 0).

        Returns:
            Tuple of (list_of_log_dicts, total_count) where total_count reflects
            the full match count before pagination is applied.
        """
        ...

    def cleanup_old_logs(self, days_to_keep: int) -> int:
        """Delete log records older than days_to_keep days.

        Args:
            days_to_keep: Records with timestamp older than this many days are deleted.

        Returns:
            Number of rows deleted.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...


# ---------------------------------------------------------------------------
# ApiMetricsBackend (Story #502: ApiMetricsBackend Protocol and SQLite Wrapper)
# ---------------------------------------------------------------------------


@runtime_checkable
class ApiMetricsBackend(Protocol):
    """Protocol for API metrics storage (Story #502).

    Supports both SQLite (standalone) and PostgreSQL (cluster) backends.
    Tracks rolling-window API call counts per category, optionally filtered
    by cluster node identifier.
    """

    def insert_metric(
        self,
        metric_type: str,
        timestamp: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> None:
        """Insert a single metric record.

        Args:
            metric_type: Category of API call ('semantic', 'other_index',
                         'regex', 'other_api').
            timestamp: ISO 8601 timestamp string. Uses current UTC time when None.
            node_id: Optional cluster node identifier (NULL in standalone).
        """
        ...

    def get_metrics(
        self,
        window_seconds: int = 3600,
        node_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return metric counts within the rolling window.

        Args:
            window_seconds: Time window in seconds (default 3600 = 1 hour).
            node_id: When provided, filter to metrics from this node only.
                     When None, aggregate across all nodes.

        Returns:
            Dict with keys: semantic_searches, other_index_searches,
            regex_searches, other_api_calls — each mapped to an integer count.
        """
        ...

    def cleanup_old(self, max_age_seconds: int = 86400) -> int:
        """Delete metric records older than max_age_seconds.

        Args:
            max_age_seconds: Records older than this many seconds are deleted
                             (default 86400 = 24 hours).

        Returns:
            Number of rows deleted.
        """
        ...

    def reset(self) -> None:
        """Delete all metric records (used for testing / manual resets)."""
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...
