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

Issue #1935 Part 2: this module used to be a single 2,077-line file. It is
now a package with one module per Protocol (mirroring the naming used by
server/storage/postgres/), re-exported here so every existing
``from ...storage.protocols import X`` import keeps working unchanged.
"""

from __future__ import annotations

from .api_metrics_backend import ApiMetricsBackend
from .audit_log_backend import AuditLogBackend
from .background_jobs_backend import BackgroundJobsBackend
from .ci_tokens_backend import CITokensBackend
from .dependency_map_tracking_backend import DependencyMapTrackingBackend
from .description_refresh_tracking_backend import DescriptionRefreshTrackingBackend
from .diagnostics_backend import DiagnosticsBackend
from .git_credentials_backend import GitCredentialsBackend
from .global_repos_backend import GlobalReposBackend
from .golden_repo_metadata_backend import GoldenRepoMetadataBackend
from .groups_backend import GroupsBackend
from .hnsw_orphan_sweep_state_backend import HNSWOrphanSweepStateBackend
from .logs_backend import LogsBackend
from .maintenance_backend import MaintenanceBackend
from .node_metrics_backend import NodeMetricsBackend
from .oauth_backend import OAuthBackend
from .payload_cache_backend import PayloadCacheBackend
from .query_embedding_cache_backend import QueryEmbeddingCacheBackend
from .refresh_token_backend import RefreshTokenBackend
from .repo_category_backend import RepoCategoryBackend
from .research_sessions_backend import ResearchSessionsBackend
from .scip_audit_backend import SCIPAuditBackend
from .self_monitoring_backend import SelfMonitoringBackend
from .sessions_backend import SessionsBackend
from .ssh_keys_backend import SSHKeysBackend
from .sync_jobs_backend import SyncJobsBackend
from .users_backend import UsersBackend
from .wiki_cache_backend import WikiCacheBackend

__all__ = [
    "ApiMetricsBackend",
    "AuditLogBackend",
    "BackgroundJobsBackend",
    "CITokensBackend",
    "DependencyMapTrackingBackend",
    "DescriptionRefreshTrackingBackend",
    "DiagnosticsBackend",
    "GitCredentialsBackend",
    "GlobalReposBackend",
    "GoldenRepoMetadataBackend",
    "GroupsBackend",
    "HNSWOrphanSweepStateBackend",
    "LogsBackend",
    "MaintenanceBackend",
    "NodeMetricsBackend",
    "OAuthBackend",
    "PayloadCacheBackend",
    "QueryEmbeddingCacheBackend",
    "RefreshTokenBackend",
    "RepoCategoryBackend",
    "ResearchSessionsBackend",
    "SCIPAuditBackend",
    "SelfMonitoringBackend",
    "SessionsBackend",
    "SSHKeysBackend",
    "SyncJobsBackend",
    "UsersBackend",
    "WikiCacheBackend",
]
