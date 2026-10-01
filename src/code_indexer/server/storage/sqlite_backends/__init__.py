"""
SQLite backend implementations for all server managers.

Story #702: Migrate Central JSON Files to SQLite

Provides SQLite-backed storage implementations that replace JSON file storage,
eliminating race conditions from concurrent GlobalRegistry instances.

Issue #1935 Part 1: this module was split from a single 8,745-line
sqlite_backends.py file into this package, one module per backend class
(mirroring server/storage/postgres/'s per-backend layout), each < 1,000
lines. Pure move -- zero behaviour change.

Re-export surface: every backend CLASS the old flat module exported is
re-exported below, so all existing `from ...sqlite_backends import X`
call sites (and direct `sqlite_backends.X` attribute access in tests)
keep working unchanged. In addition, the following project-owned
module-level names that were importable from the old flat module (via
its own `import`/definitions) are ALSO re-exported for surface parity,
even though no in-repo consumer currently imports them through this
package: DatabaseConnectionManager, _UNSET, _RECONCILE_AUTO_HEAL_EVENT_ROW_ID,
_HNSW_SWEEP_OUTCOMES, _HNSW_SWEEP_OUTCOME_COLUMNS, and the four
GoldenRepoMetadataSqliteBackend dedup-outcome helpers
(_dedup_state_row_to_dict, _insert_new_dedup_row,
_update_existing_dedup_row, _apply_dedup_outcome_upsert).

Deliberately NOT re-exported: the stdlib/typing names the old flat
module merely `import`ed for its own use (os, json, sqlite3, math,
datetime, timedelta, timezone, MappingProxyType, and the `typing`
aliases Any/Dict/List/Optional/Set/Tuple). These were never
project-owned symbols -- they are equally available to any caller
directly from their own stdlib/typing modules, and no in-repo consumer
ever imported them through sqlite_backends. Re-exporting them here
would add noise without restoring any real capability.
"""

import logging

import psutil

from ..database_manager import DatabaseConnectionManager
from .api_metrics_backend import ApiMetricsSqliteBackend, PERIOD_TO_TIER
from .background_jobs_backend import (
    BackgroundJobsSqliteBackend,
    _TERMINAL_JOB_STATUSES,
    _owning_worker_process_is_alive,
)
from .ci_tokens_backend import CITokensSqliteBackend
from .dependency_map_dashboard_cache_backend import (
    DependencyMapDashboardCacheBackend,
)
from .dependency_map_tracking_backend import DependencyMapTrackingBackend, _UNSET
from .description_refresh_tracking_backend import DescriptionRefreshTrackingBackend
from .diagnostics_backend import DiagnosticsSqliteBackend
from .git_credentials_backend import GitCredentialsSqliteBackend
from .global_repos_backend import GlobalReposSqliteBackend
from .golden_repo_metadata_backend import GoldenRepoMetadataSqliteBackend
from ._golden_repo_metadata_extra_mixin import (
    _RECONCILE_AUTO_HEAL_EVENT_ROW_ID,
    _dedup_state_row_to_dict,
    _insert_new_dedup_row,
    _update_existing_dedup_row,
    _apply_dedup_outcome_upsert,
)
from .hidden_discovery_repos_backend import HiddenDiscoveryReposSqliteBackend
from .hnsw_orphan_sweep_state_backend import (
    HNSWOrphanSweepStateSqliteBackend,
    _HNSW_SWEEP_OUTCOMES,
    _HNSW_SWEEP_OUTCOME_COLUMNS,
)
from .logs_backend import LogsSqliteBackend
from .maintenance_backend import MaintenanceSqliteBackend
from .node_metrics_backend import NodeMetricsSqliteBackend
from .oauth_backend import OAuthSqliteBackend
from .payload_cache_backend import PayloadCacheSqliteBackend
from .query_embedding_cache_backend import QueryEmbeddingCacheSqliteBackend
from .refresh_token_backend import RefreshTokenSqliteBackend
from .research_sessions_backend import ResearchSessionsSqliteBackend
from .scip_audit_backend import SCIPAuditSqliteBackend
from .self_monitoring_backend import SelfMonitoringSqliteBackend
from .sessions_backend import SessionsSqliteBackend
from .ssh_keys_backend import SSHKeysSqliteBackend
from .sync_jobs_backend import SyncJobsSqliteBackend
from .users_backend import UsersSqliteBackend
from .wiki_cache_backend import WikiCacheSqliteBackend

logger = logging.getLogger(__name__)

__all__ = [
    "ApiMetricsSqliteBackend",
    "PERIOD_TO_TIER",
    "BackgroundJobsSqliteBackend",
    "CITokensSqliteBackend",
    "DependencyMapDashboardCacheBackend",
    "DependencyMapTrackingBackend",
    "DescriptionRefreshTrackingBackend",
    "DiagnosticsSqliteBackend",
    "GitCredentialsSqliteBackend",
    "GlobalReposSqliteBackend",
    "GoldenRepoMetadataSqliteBackend",
    "HiddenDiscoveryReposSqliteBackend",
    "HNSWOrphanSweepStateSqliteBackend",
    "LogsSqliteBackend",
    "MaintenanceSqliteBackend",
    "NodeMetricsSqliteBackend",
    "OAuthSqliteBackend",
    "PayloadCacheSqliteBackend",
    "QueryEmbeddingCacheSqliteBackend",
    "RefreshTokenSqliteBackend",
    "ResearchSessionsSqliteBackend",
    "SCIPAuditSqliteBackend",
    "SelfMonitoringSqliteBackend",
    "SessionsSqliteBackend",
    "SSHKeysSqliteBackend",
    "SyncJobsSqliteBackend",
    "UsersSqliteBackend",
    "WikiCacheSqliteBackend",
    "psutil",
    "logger",
    "_owning_worker_process_is_alive",
    "_TERMINAL_JOB_STATUSES",
    "DatabaseConnectionManager",
    "_UNSET",
    "_RECONCILE_AUTO_HEAL_EVENT_ROW_ID",
    "_dedup_state_row_to_dict",
    "_insert_new_dedup_row",
    "_update_existing_dedup_row",
    "_apply_dedup_outcome_upsert",
    "_HNSW_SWEEP_OUTCOMES",
    "_HNSW_SWEEP_OUTCOME_COLUMNS",
]
