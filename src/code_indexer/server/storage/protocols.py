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
        progress_info: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
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

    def cleanup_old_completed(self, cutoff_iso: str) -> int:
        """Delete completed or failed sync jobs older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; jobs with completed_at before
                        this value and status IN ('completed', 'failed') are deleted.

        Returns:
            Number of rows deleted.
        """
        ...

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

    def cleanup_old_history(self, cutoff_iso: str) -> int:
        """Delete dependency_map_run_history records older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; records with timestamp before
                        this value are deleted.

        Returns:
            Number of rows deleted.
        """
        ...

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

    def cleanup_old_logs(self, cutoff_iso: str) -> int:
        """Delete audit log records older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; records with timestamp before
                        this value are deleted.

        Returns:
            Number of rows deleted.
        """
        ...


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
        username: str = "_anonymous",
    ) -> None:
        """Insert a single metric record.

        Args:
            metric_type: Category of API call ('semantic', 'other_index',
                         'regex', 'other_api').
            timestamp: ISO 8601 timestamp string. Uses current UTC time when None.
            node_id: Optional cluster node identifier (NULL in standalone).
            username: Username for bucket attribution. Defaults to '_anonymous'.
        """
        ...

    def upsert_bucket(
        self,
        username: str,
        granularity: str,
        bucket_start: str,
        metric_type: str,
    ) -> None:
        """Upsert a single bucket row, incrementing count by 1.

        Args:
            username: Username for attribution (e.g. 'alice', '_anonymous').
            granularity: One of 'min1', 'min5', 'hour1', 'day1'.
            bucket_start: ISO 8601 timestamp of the bucket start boundary.
            metric_type: Category ('semantic', 'other_index', 'regex', 'other_api').
        """
        ...

    def cleanup_expired_buckets(self) -> None:
        """Delete expired bucket rows per granularity retention policy.

        Retention:
            min1  — 15 minutes
            min5  — 1 hour
            hour1 — 24 hours
            day1  — 15 days
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

    def get_metrics_bucketed(
        self,
        period_seconds: int,
        username: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return metric totals from api_metrics_buckets for the given period.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.
            username: When provided, filter to this user's rows only.
                      When None, aggregate across all users.

        Returns:
            Dict with keys: semantic, other_index, regex, other_api.
        """
        ...

    def get_metrics_by_user(
        self,
        period_seconds: int,
    ) -> Dict[str, Dict[str, int]]:
        """Return per-user metric totals from api_metrics_buckets for the given period.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.

        Returns:
            Dict mapping username to {metric_type: count}.
        """
        ...

    def get_metrics_timeseries(
        self,
        period_seconds: int,
    ) -> List[Tuple[str, str, int]]:
        """Return timeseries data from api_metrics_buckets for the given period.

        Args:
            period_seconds: Duration in seconds. Must be a key in PERIOD_TO_TIER.

        Returns:
            List of (bucket_start, metric_type, count) ordered by bucket_start ASC.
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


# ---------------------------------------------------------------------------
# PayloadCacheBackend (Story #504: PayloadCacheBackend Protocol and Backends)
# ---------------------------------------------------------------------------


@runtime_checkable
class PayloadCacheBackend(Protocol):
    """Protocol for payload cache storage (Story #504).

    Supports both SQLite (standalone) and PostgreSQL (cluster) backends.
    Stores large content with TTL-based eviction, keyed by a cache handle.
    """

    def store(
        self,
        cache_handle: str,
        content: str,
        preview: str,
        ttl_seconds: int,
        node_id: Optional[str] = None,
    ) -> None:
        """Store a payload cache entry.

        Args:
            cache_handle: Unique identifier for this cache entry.
            content: Full content to cache.
            preview: Truncated preview of the content.
            ttl_seconds: Time-to-live in seconds.
            node_id: Optional cluster node identifier (NULL in standalone).
        """
        ...

    def retrieve(self, cache_handle: str) -> Optional[Dict[str, Any]]:
        """Retrieve a cache entry by handle, or None if missing or expired.

        Args:
            cache_handle: Unique identifier for the cache entry.

        Returns:
            Dict with keys: content, preview, created_at, node_id — or None
            if the entry does not exist or has exceeded its TTL.
        """
        ...

    def cleanup_expired(self) -> int:
        """Delete all entries that have exceeded their TTL.

        Returns:
            Number of rows deleted.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...


# ---------------------------------------------------------------------------
# OAuthBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class OAuthBackend(Protocol):
    """
    Protocol for OAuth 2.1 token and client storage.

    Satisfies PEP 544 structural subtyping: any class implementing all
    of these methods is accepted as an OAuthBackend without inheritance.
    """

    def register_client(
        self,
        client_name: str,
        redirect_uris: List[str],
        grant_types: Optional[List[str]] = None,
        response_types: Optional[List[str]] = None,
        token_endpoint_auth_method: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register a new OAuth client and return its registration data.

        Args:
            client_name: Human-readable name for the client.
            redirect_uris: List of allowed redirect URIs.
            grant_types: Allowed grant types (default: authorization_code, refresh_token).
            response_types: Allowed response types (default: code).
            token_endpoint_auth_method: Auth method (default: none).
            scope: Optional scope string.

        Returns:
            Dict with client_id and registration metadata.
        """
        ...

    def get_client(self, client_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a registered client by its client_id.

        Args:
            client_id: The client identifier.

        Returns:
            Dict with client data, or None if not found.
        """
        ...

    def generate_authorization_code(
        self,
        client_id: str,
        user_id: str,
        code_challenge: str,
        redirect_uri: str,
        state: Optional[str] = None,
    ) -> str:
        """Generate a one-time PKCE authorization code.

        Args:
            client_id: The registered OAuth client ID.
            user_id: The authenticated user identifier.
            code_challenge: PKCE S256 code challenge.
            redirect_uri: The redirect URI for this request.
            state: Opaque state value from the authorization request.

        Returns:
            A short-lived authorization code string.
        """
        ...

    def exchange_code_for_token(
        self, code: str, code_verifier: str, client_id: str
    ) -> Dict[str, Any]:
        """Exchange a PKCE authorization code for access and refresh tokens.

        Args:
            code: The authorization code to exchange.
            code_verifier: PKCE code verifier for S256 verification.
            client_id: The OAuth client requesting the exchange.

        Returns:
            Dict with access_token, token_type, expires_in, refresh_token.
        """
        ...

    def validate_token(self, access_token: str) -> Optional[Dict[str, Any]]:
        """Validate an access token and return its associated data.

        Args:
            access_token: The bearer token to validate.

        Returns:
            Dict with token_id, client_id, user_id, expires_at, created_at —
            or None if the token is unknown or expired.
        """
        ...

    def extend_token_on_activity(self, access_token: str) -> bool:
        """Extend an access token's expiry if it is within the extension threshold.

        Args:
            access_token: The bearer token to potentially extend.

        Returns:
            True if the token was extended, False otherwise.
        """
        ...

    def refresh_access_token(
        self, refresh_token: str, client_id: str
    ) -> Dict[str, Any]:
        """Exchange a refresh token for new access and refresh tokens.

        Args:
            refresh_token: The refresh token to exchange.
            client_id: The OAuth client requesting the refresh.

        Returns:
            Dict with access_token, token_type, expires_in, refresh_token.
        """
        ...

    def revoke_token(
        self, token: str, token_type_hint: Optional[str] = None
    ) -> Dict[str, Optional[str]]:
        """Revoke an access or refresh token.

        Args:
            token: The token to revoke.
            token_type_hint: Optional hint ('access_token' or 'refresh_token').

        Returns:
            Dict with username and token_type if found, None values otherwise.
        """
        ...

    def handle_client_credentials_grant(
        self,
        client_id: str,
        client_secret: str,
        scope: Optional[str] = None,
        mcp_credential_manager: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Handle OAuth 2.1 client_credentials grant type.

        Args:
            client_id: MCP client ID.
            client_secret: MCP client secret.
            scope: Optional scope string.
            mcp_credential_manager: MCPCredentialManager for credential verification.

        Returns:
            Dict with access_token, token_type, expires_in.
        """
        ...

    def link_oidc_identity(
        self, username: str, subject: str, email: Optional[str] = None
    ) -> None:
        """Link an OIDC subject to a local username.

        Args:
            username: Local CIDX username.
            subject: OIDC subject identifier (sub claim).
            email: Optional email address from OIDC provider.
        """
        ...

    def get_oidc_identity(self, subject: str) -> Optional[Dict[str, Any]]:
        """Retrieve an OIDC identity link by subject.

        Args:
            subject: OIDC subject identifier (sub claim).

        Returns:
            Dict with username, subject, email — or None if not found.
        """
        ...

    def delete_oidc_identity(self, subject: str) -> None:
        """Delete a stale OIDC identity link by subject.

        Args:
            subject: OIDC subject identifier (sub claim) to delete.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...


# ---------------------------------------------------------------------------
# RefreshTokenBackend (Story #515)
# ---------------------------------------------------------------------------


@runtime_checkable
class RefreshTokenBackend(Protocol):
    """Protocol for refresh token storage (Story #515).

    Provides data-level access to token_families and refresh_tokens tables.
    Satisfies PEP 544 structural subtyping: any class implementing all of
    these methods is accepted as a RefreshTokenBackend without inheritance.
    """

    # token_families table

    def create_token_family(
        self, family_id: str, username: str, created_at: str, last_used_at: str
    ) -> None: ...

    def get_token_family(self, family_id: str) -> Optional[Dict[str, Any]]: ...

    def revoke_token_family(self, family_id: str, reason: str) -> None: ...

    def revoke_user_families(self, username: str, reason: str) -> int: ...

    def update_family_last_used(self, family_id: str, last_used_at: str) -> None: ...

    # refresh_tokens table

    def store_refresh_token(
        self,
        token_id: str,
        family_id: str,
        username: str,
        token_hash: str,
        created_at: str,
        expires_at: str,
        parent_token_id: Optional[str] = None,
    ) -> None: ...

    def get_refresh_token_by_hash(
        self, token_hash: str
    ) -> Optional[Dict[str, Any]]: ...

    def mark_token_used(self, token_id: str, used_at: str) -> None: ...

    def count_active_tokens_in_family(self, family_id: str) -> int: ...

    def delete_expired_tokens(self, now_iso: str) -> int: ...

    def delete_orphaned_families(self) -> int: ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...


# ---------------------------------------------------------------------------
# SCIPAuditBackend (Story #516: SCIPAuditBackend for Cluster Node Identification)
# ---------------------------------------------------------------------------


@runtime_checkable
class SCIPAuditBackend(Protocol):
    """Protocol for SCIP audit storage (Story #516).

    Supports both SQLite (standalone) and PostgreSQL (cluster) backends.
    Tracks dependency installations with project context and cluster node
    identification.
    """

    def create_audit_record(
        self,
        job_id: str,
        repo_alias: str,
        package: str,
        command: str,
        project_path: Optional[str] = None,
        project_language: Optional[str] = None,
        project_build_system: Optional[str] = None,
        reasoning: Optional[str] = None,
        username: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> int:
        """Create an audit record for a dependency installation.

        Args:
            job_id: Background job ID that triggered installation.
            repo_alias: Repository alias being processed.
            package: Package name that was installed.
            command: Full installation command executed.
            project_path: Project path within repository (optional).
            project_language: Programming language (optional).
            project_build_system: Build system used (optional).
            reasoning: Claude's reasoning for installation (optional).
            username: User who triggered the job (optional).
            node_id: Cluster node identifier (optional, Story #516 AC1).

        Returns:
            Record ID of created audit record.
        """
        ...

    def query_audit_records(
        self,
        job_id: Optional[str] = None,
        repo_alias: Optional[str] = None,
        project_language: Optional[str] = None,
        project_build_system: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> "Tuple[List[Dict[str, Any]], int]":
        """Query audit records with filtering and pagination.

        Args:
            job_id: Filter by job ID (optional).
            repo_alias: Filter by repository alias (optional).
            project_language: Filter by project language (optional).
            project_build_system: Filter by build system (optional).
            since: Filter records after this ISO timestamp (optional).
            until: Filter records before this ISO timestamp (optional).
            limit: Maximum records to return (default 100).
            offset: Number of records to skip (default 0).

        Returns:
            Tuple of (records list, total count).
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...


# ---------------------------------------------------------------------------
# ResearchSessionsBackend (Story #522)
# ---------------------------------------------------------------------------


@runtime_checkable
class ResearchSessionsBackend(Protocol):
    """Protocol for research sessions storage (Story #522).

    Provides data-level access to research_sessions and research_messages tables.
    Satisfies PEP 544 structural subtyping: any class implementing all of
    these methods is accepted as a ResearchSessionsBackend without inheritance.
    """

    def create_session(
        self,
        session_id: str,
        name: str,
        folder_path: str,
        claude_session_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> None: ...

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]: ...

    def list_sessions(self) -> List[Dict[str, Any]]: ...

    def delete_session(self, session_id: str) -> bool: ...

    def update_session_title(self, session_id: str, name: str) -> bool: ...

    def update_session_claude_id(
        self, session_id: str, claude_session_id: str
    ) -> None: ...

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]: ...

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# DiagnosticsBackend (Story #525)
# ---------------------------------------------------------------------------


@runtime_checkable
class DiagnosticsBackend(Protocol):
    """Protocol for diagnostics results storage (Story #525).

    Provides data-level access to the diagnostic_results table.
    Satisfies PEP 544 structural subtyping: any class implementing all of
    these methods is accepted as a DiagnosticsBackend without inheritance.
    """

    def save_results(self, category: str, results_json: str, run_at: str) -> None:
        """Persist (upsert) diagnostic results for a category."""
        ...

    def load_all_results(self) -> "List[Tuple[str, str, str]]":
        """Return all rows as list of (category, results_json, run_at) tuples."""
        ...

    def load_category_results(self, category: str) -> "Optional[Tuple[str, str]]":
        """Return (results_json, run_at) for a category, or None if absent."""
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...


# ---------------------------------------------------------------------------
# SelfMonitoringBackend (Story #524)
# ---------------------------------------------------------------------------


@runtime_checkable
class SelfMonitoringBackend(Protocol):
    """Protocol for self-monitoring storage (Story #524).

    Provides data-level access to self_monitoring_scans and
    self_monitoring_issues tables.
    Satisfies PEP 544 structural subtyping: any class implementing all of
    these methods is accepted as a SelfMonitoringBackend without inheritance.
    """

    def create_scan_record(
        self,
        scan_id: str,
        started_at: str,
        log_id_start: int,
    ) -> None:
        """Insert initial scan record with RUNNING status."""
        ...

    def get_last_scan_log_id(self) -> int:
        """Return log_id_end from most recent SUCCESS scan, or 0."""
        ...

    def update_scan_record(
        self,
        scan_id: str,
        status: str,
        completed_at: str,
        log_id_end: "Optional[int]" = None,
        issues_created: "Optional[int]" = None,
        error_message: "Optional[str]" = None,
    ) -> None:
        """Update scan record with completion status and metrics."""
        ...

    def cleanup_orphaned_scans(self, cutoff_iso: str) -> int:
        """Mark scans started before cutoff_iso with no completed_at as FAILURE.

        Returns count of scans updated.
        """
        ...

    def get_last_started_at(self) -> "Optional[str]":
        """Return started_at from most recent scan (any status), or None."""
        ...

    def fetch_stored_fingerprints(
        self, retention_days: int
    ) -> "List[Tuple[str, str, str, str, str]]":
        """Return fingerprint rows (fingerprint, classification, error_codes, title, created_at)."""
        ...

    def store_issue_metadata(
        self,
        scan_id: str,
        github_issue_number: "Optional[int]",
        github_issue_url: "Optional[str]",
        classification: str,
        title: str,
        error_codes: str,
        fingerprint: str,
        source_log_ids: str,
        source_files: str,
        created_at: str,
    ) -> None:
        """Persist issue metadata in self_monitoring_issues."""
        ...

    def list_scans(self, limit: int = 50) -> "List[Dict[str, Any]]":
        """Return scan history records, most recent first.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of dicts with keys: scan_id, started_at, completed_at, status,
            log_id_start, log_id_end, issues_created, error_message.
        """
        ...

    def list_issues(self, limit: int = 100) -> "List[Dict[str, Any]]":
        """Return issue records, most recent first.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of dicts with keys: id, scan_id, github_issue_number,
            github_issue_url, classification, title, fingerprint,
            source_log_ids, source_files, created_at.
        """
        ...

    def get_running_scan_count(self) -> int:
        """Return count of scans where completed_at IS NULL (currently running).

        Returns:
            Integer count of running scans.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...


# ---------------------------------------------------------------------------
# WikiCacheBackend (Story #523)
# ---------------------------------------------------------------------------


@runtime_checkable
class WikiCacheBackend(Protocol):
    """Protocol for wiki cache storage (Story #523).

    Provides data-level access to wiki_cache, wiki_sidebar_cache, and
    wiki_article_views tables.
    Satisfies PEP 544 structural subtyping: any class implementing all of
    these methods is accepted as a WikiCacheBackend without inheritance.
    """

    def get_article(
        self, repo_alias: str, article_path: str
    ) -> "Optional[Dict[str, Any]]":
        """Return dict with rendered_html, title, file_mtime, file_size, metadata_json or None."""
        ...

    def put_article(
        self,
        repo_alias: str,
        article_path: str,
        html: str,
        title: str,
        file_mtime: float,
        file_size: int,
        rendered_at: str,
        metadata_json: "Optional[str]",
    ) -> None:
        """Store (upsert) rendered article row."""
        ...

    def get_sidebar(self, repo_alias: str) -> "Optional[str]":
        """Return sidebar_json string for repo_alias, or None."""
        ...

    def put_sidebar(
        self,
        repo_alias: str,
        sidebar_json: str,
        max_mtime: float,
        built_at: str,
    ) -> None:
        """Store (upsert) sidebar row."""
        ...

    def invalidate_repo(self, repo_alias: str) -> None:
        """Delete all wiki_cache and wiki_sidebar_cache rows for repo_alias."""
        ...

    def increment_view(self, repo_alias: str, article_path: str, now: str) -> None:
        """Upsert wiki_article_views, incrementing real_views."""
        ...

    def get_view_count(self, repo_alias: str, article_path: str) -> int:
        """Return real_views count for article, or 0."""
        ...

    def get_all_view_counts(self, repo_alias: str) -> "List[Dict[str, Any]]":
        """Return all view records for repo as list of dicts."""
        ...

    def delete_views_for_repo(self, repo_alias: str) -> None:
        """Delete all wiki_article_views rows for repo_alias."""
        ...

    def insert_initial_views(
        self, repo_alias: str, article_path: str, views: int, now: str
    ) -> None:
        """Insert initial view count (INSERT OR IGNORE)."""
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...


# ---------------------------------------------------------------------------
# MaintenanceBackend (Story #529)
# ---------------------------------------------------------------------------


@runtime_checkable
class MaintenanceBackend(Protocol):
    """Protocol for maintenance mode state storage (Story #529).

    Provides cluster-wide coordination of maintenance mode by persisting
    state to the shared storage backend (PostgreSQL in cluster mode,
    SQLite in standalone mode).

    Satisfies PEP 544 structural subtyping: any class implementing all of
    these methods is accepted as a MaintenanceBackend without inheritance.
    """

    def enter_maintenance(self, started_by: str, reason: str, started_at: str) -> None:
        """Persist maintenance mode as active.

        Args:
            started_by: Username or identifier of who activated maintenance mode.
            reason: Human-readable reason for entering maintenance mode.
            started_at: ISO 8601 timestamp when maintenance mode was activated.
        """
        ...

    def exit_maintenance(self) -> None:
        """Mark maintenance mode as inactive (disable it)."""
        ...

    def get_status(self) -> "Optional[Dict[str, Any]]":
        """Return current maintenance state.

        Returns:
            Dict with keys: enabled (bool), reason (str or None),
            started_at (str or None), started_by (str or None).
            Always returns a dict (never None); enabled=False when inactive.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...
