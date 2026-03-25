"""
StorageFactory: single point of backend instantiation for CIDX server.

Story #417: Factory Pattern for Backend Selection (SQLite vs PostgreSQL)

Reads ``storage_mode`` from the server config dict and creates all storage
backend instances, returning them as a typed BackendRegistry.

Backward compatibility guarantee:
- When ``storage_mode`` is absent or ``"sqlite"``, the factory instantiates
  the exact same SQLite backends used before this story.
- psycopg imports are LAZY: they only happen inside _create_postgres_backends().
  Standalone (SQLite-only) servers never import psycopg.
- Zero behaviour change for standalone servers.

Usage:
    from code_indexer.server.storage.factory import StorageFactory

    registry = StorageFactory.create_backends(config=config_dict, data_dir="/path/to/data")
    registry.global_repos.register_repo(...)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from code_indexer.server.storage.protocols import (
        ApiMetricsBackend,
        AuditLogBackend,
        BackgroundJobsBackend,
        CITokensBackend,
        DependencyMapTrackingBackend,
        DescriptionRefreshTrackingBackend,
        GitCredentialsBackend,
        GlobalReposBackend,
        GoldenRepoMetadataBackend,
        GroupsBackend,
        LogsBackend,
        NodeMetricsBackend,
        OAuthBackend,
        PayloadCacheBackend,
        RefreshTokenBackend,
        RepoCategoryBackend,
        SCIPAuditBackend,
        SessionsBackend,
        SSHKeysBackend,
        SyncJobsBackend,
        UsersBackend,
    )


# ---------------------------------------------------------------------------
# BackendRegistry
# ---------------------------------------------------------------------------


@dataclass
class BackendRegistry:
    """
    Container for all storage backend instances, typed by Protocol.

    Every field satisfies the corresponding Protocol from protocols.py,
    allowing callers to be storage-agnostic.
    """

    global_repos: "GlobalReposBackend"
    users: "UsersBackend"
    sessions: "SessionsBackend"
    background_jobs: "BackgroundJobsBackend"
    sync_jobs: "SyncJobsBackend"
    ci_tokens: "CITokensBackend"
    description_refresh_tracking: "DescriptionRefreshTrackingBackend"
    ssh_keys: "SSHKeysBackend"
    golden_repo_metadata: "GoldenRepoMetadataBackend"
    dependency_map_tracking: "DependencyMapTrackingBackend"
    git_credentials: "GitCredentialsBackend"
    repo_category: "RepoCategoryBackend"
    groups: "GroupsBackend"
    audit_log: "AuditLogBackend"
    node_metrics: "NodeMetricsBackend"
    logs: "LogsBackend"
    api_metrics: "ApiMetricsBackend"
    payload_cache: "PayloadCacheBackend"
    oauth: "OAuthBackend"
    scip_audit: "SCIPAuditBackend"
    refresh_tokens: "RefreshTokenBackend"


# ---------------------------------------------------------------------------
# StorageFactory
# ---------------------------------------------------------------------------


class StorageFactory:
    """
    Factory that reads ``storage_mode`` from config and returns a BackendRegistry.

    Supported modes:
    - ``"sqlite"`` (default when key is absent): local SQLite files under data_dir
    - ``"postgres"``: PostgreSQL via psycopg v3, DSN read from ``postgres_dsn``
    """

    @staticmethod
    def create_backends(config: Dict[str, Any], data_dir: str) -> BackendRegistry:
        """
        Create and return all storage backends.

        Args:
            config: Server configuration dict (typically from config.json).
                    Reads ``storage_mode`` (default ``"sqlite"``) and, when
                    postgres mode is selected, ``postgres_dsn``.
            data_dir: Path to the server data directory used by SQLite backends
                      (e.g. ``~/.cidx-server/data``).

        Returns:
            BackendRegistry with all backend instances fully initialised.

        Raises:
            ImportError: When postgres mode is selected but psycopg is not installed.
            ValueError: When ``storage_mode`` is an unrecognised value.
        """
        storage_mode = config.get("storage_mode", "sqlite")
        if storage_mode == "postgres":
            return StorageFactory._create_postgres_backends(config)
        elif storage_mode == "sqlite":
            return StorageFactory._create_sqlite_backends(data_dir)
        else:
            raise ValueError(
                f"Unsupported storage_mode: {storage_mode!r}. "
                "Valid values are 'sqlite' and 'postgres'."
            )

    # ------------------------------------------------------------------
    # SQLite path
    # ------------------------------------------------------------------

    @staticmethod
    def _create_sqlite_backends(data_dir: str) -> BackendRegistry:
        """Instantiate all SQLite backends exactly as done today in service_init."""
        from code_indexer.server.storage.sqlite_backends import (
            ApiMetricsSqliteBackend,
            BackgroundJobsSqliteBackend,
            CITokensSqliteBackend,
            DependencyMapTrackingBackend as DependencyMapTrackingBackend_,
            DescriptionRefreshTrackingBackend as DescriptionRefreshTrackingBackend_,
            GitCredentialsSqliteBackend,
            GlobalReposSqliteBackend,
            GoldenRepoMetadataSqliteBackend,
            LogsSqliteBackend,
            NodeMetricsSqliteBackend,
            OAuthSqliteBackend,
            PayloadCacheSqliteBackend,
            RefreshTokenSqliteBackend,
            SCIPAuditSqliteBackend,
            SessionsSqliteBackend,
            SSHKeysSqliteBackend,
            SyncJobsSqliteBackend,
            UsersSqliteBackend,
        )
        from code_indexer.server.storage.repo_category_backend import (
            RepoCategorySqliteBackend,
        )
        from code_indexer.server.services.group_access_manager import GroupAccessManager
        from code_indexer.server.services.audit_log_service import AuditLogService

        # Main database: all backends except groups/audit share the same DB.
        db_path = str(Path(data_dir) / "cidx_server.db")

        # Groups and audit log use a dedicated database (matches lifespan.py pattern).
        groups_db_path = Path(data_dir).parent / "groups.db"

        return BackendRegistry(
            global_repos=GlobalReposSqliteBackend(db_path),
            users=UsersSqliteBackend(db_path),
            sessions=SessionsSqliteBackend(db_path),
            background_jobs=BackgroundJobsSqliteBackend(db_path),
            sync_jobs=SyncJobsSqliteBackend(db_path),
            ci_tokens=CITokensSqliteBackend(db_path),
            description_refresh_tracking=DescriptionRefreshTrackingBackend_(db_path),
            ssh_keys=SSHKeysSqliteBackend(db_path),
            golden_repo_metadata=GoldenRepoMetadataSqliteBackend(db_path),
            dependency_map_tracking=DependencyMapTrackingBackend_(db_path),
            git_credentials=GitCredentialsSqliteBackend(db_path),
            repo_category=RepoCategorySqliteBackend(db_path),
            groups=GroupAccessManager(groups_db_path),
            audit_log=AuditLogService(groups_db_path),
            node_metrics=NodeMetricsSqliteBackend(db_path),
            logs=LogsSqliteBackend(db_path=str(Path(data_dir).parent / "logs.db")),
            api_metrics=ApiMetricsSqliteBackend(
                db_path=str(Path(data_dir) / "api_metrics.db")
            ),
            payload_cache=PayloadCacheSqliteBackend(
                db_path=str(Path(data_dir) / "payload_cache.db")
            ),
            oauth=OAuthSqliteBackend(db_path=str(Path(data_dir).parent / "oauth.db")),
            scip_audit=SCIPAuditSqliteBackend(
                db_path=str(Path(data_dir).parent / "scip_audit.db")
            ),
            refresh_tokens=RefreshTokenSqliteBackend(
                db_path=str(Path(data_dir).parent / "refresh_tokens.db")
            ),
        )

    # ------------------------------------------------------------------
    # PostgreSQL path  (lazy import — psycopg only loaded here)
    # ------------------------------------------------------------------

    @staticmethod
    def _create_postgres_backends(config: Dict[str, Any]) -> BackendRegistry:
        """
        Instantiate all PostgreSQL backends.

        psycopg is imported lazily inside this method so that standalone
        (SQLite-only) servers never need psycopg installed.

        Args:
            config: Server config dict; must contain ``postgres_dsn``.

        Raises:
            ImportError: If psycopg is not installed.
            KeyError: If ``postgres_dsn`` is missing from config.
        """
        # Lazy import — will raise ImportError if psycopg not installed.
        from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
        from code_indexer.server.storage.postgres.global_repos_backend import (
            GlobalReposPostgresBackend,
        )
        from code_indexer.server.storage.postgres.users_backend import (
            UsersPostgresBackend,
        )
        from code_indexer.server.storage.postgres.sessions_backend import (
            SessionsPostgresBackend,
        )
        from code_indexer.server.storage.postgres.background_jobs_backend import (
            BackgroundJobsPostgresBackend,
        )
        from code_indexer.server.storage.postgres.sync_jobs_backend import (
            SyncJobsPostgresBackend,
        )
        from code_indexer.server.storage.postgres.ci_tokens_backend import (
            CITokensPostgresBackend,
        )
        from code_indexer.server.storage.postgres.description_refresh_tracking_backend import (
            DescriptionRefreshTrackingPostgresBackend,
        )
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )
        from code_indexer.server.storage.postgres.dependency_map_tracking_backend import (
            DependencyMapTrackingPostgresBackend,
        )
        from code_indexer.server.storage.postgres.git_credentials_backend import (
            GitCredentialsPostgresBackend,
        )
        from code_indexer.server.storage.postgres.repo_category_backend import (
            RepoCategoryPostgresBackend,
        )
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )
        from code_indexer.server.storage.postgres.audit_log_backend import (
            AuditLogPostgresBackend,
        )
        from code_indexer.server.storage.postgres.node_metrics_backend import (
            NodeMetricsPostgresBackend,
        )
        from code_indexer.server.storage.postgres.logs_backend import (
            LogsPostgresBackend,
        )
        from code_indexer.server.storage.postgres.api_metrics_backend import (
            ApiMetricsPostgresBackend,
        )
        from code_indexer.server.storage.postgres.payload_cache_backend import (
            PayloadCachePostgresBackend,
        )
        from code_indexer.server.storage.postgres.oauth_backend import (
            OAuthPostgresBackend,
        )
        from code_indexer.server.storage.postgres.scip_audit_backend import (
            SCIPAuditPostgresBackend,
        )
        from code_indexer.server.storage.postgres.refresh_token_backend import (
            RefreshTokenPostgresBackend,
        )

        dsn = config["postgres_dsn"]
        pool = ConnectionPool(dsn)

        return BackendRegistry(
            global_repos=GlobalReposPostgresBackend(pool),
            users=UsersPostgresBackend(pool),
            sessions=SessionsPostgresBackend(pool),
            background_jobs=BackgroundJobsPostgresBackend(pool),
            sync_jobs=SyncJobsPostgresBackend(pool),
            ci_tokens=CITokensPostgresBackend(pool),
            description_refresh_tracking=DescriptionRefreshTrackingPostgresBackend(
                pool
            ),
            ssh_keys=SSHKeysPostgresBackend(pool),
            golden_repo_metadata=GoldenRepoMetadataPostgresBackend(pool),
            dependency_map_tracking=DependencyMapTrackingPostgresBackend(pool),
            git_credentials=GitCredentialsPostgresBackend(pool),
            repo_category=RepoCategoryPostgresBackend(pool),
            groups=GroupsPostgresBackend(pool),
            audit_log=AuditLogPostgresBackend(pool),
            node_metrics=NodeMetricsPostgresBackend(pool),
            logs=LogsPostgresBackend(pool),
            api_metrics=ApiMetricsPostgresBackend(pool),
            payload_cache=PayloadCachePostgresBackend(pool),
            oauth=OAuthPostgresBackend(pool),
            scip_audit=SCIPAuditPostgresBackend(pool),
            refresh_tokens=RefreshTokenPostgresBackend(pool),
        )
