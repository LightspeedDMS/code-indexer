import sqlite3
import threading
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.storage.database_manager import (
    DatabaseConnectionManager,
    DatabaseSchema,
)
from code_indexer.server.storage.sqlite_backends.background_jobs_backend import (
    BackgroundJobsSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.ci_tokens_backend import (
    CITokensSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.dependency_map_tracking_backend import (
    DependencyMapTrackingBackend,
)
from code_indexer.server.storage.sqlite_backends.description_refresh_tracking_backend import (
    DescriptionRefreshTrackingBackend,
)
from code_indexer.server.storage.sqlite_backends.diagnostics_backend import (
    DiagnosticsSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.git_credentials_backend import (
    GitCredentialsSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.global_repos_backend import (
    GlobalReposSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.golden_repo_metadata_backend import (
    GoldenRepoMetadataSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.maintenance_backend import (
    MaintenanceSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.node_metrics_backend import (
    NodeMetricsSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.refresh_token_backend import (
    RefreshTokenSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.research_sessions_backend import (
    ResearchSessionsSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.self_monitoring_backend import (
    SelfMonitoringSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.sessions_backend import (
    SessionsSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.ssh_keys_backend import (
    SSHKeysSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.sync_jobs_backend import (
    SyncJobsSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.users_backend import (
    UsersSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.wiki_cache_backend import (
    WikiCacheSqliteBackend,
)

BACKENDS = [
    BackgroundJobsSqliteBackend,
    CITokensSqliteBackend,
    DependencyMapTrackingBackend,
    DescriptionRefreshTrackingBackend,
    DiagnosticsSqliteBackend,
    GitCredentialsSqliteBackend,
    GlobalReposSqliteBackend,
    GoldenRepoMetadataSqliteBackend,
    MaintenanceSqliteBackend,
    NodeMetricsSqliteBackend,
    RefreshTokenSqliteBackend,
    ResearchSessionsSqliteBackend,
    SelfMonitoringSqliteBackend,
    SessionsSqliteBackend,
    SSHKeysSqliteBackend,
    SyncJobsSqliteBackend,
    UsersSqliteBackend,
    WikiCacheSqliteBackend,
]


@pytest.mark.parametrize("backend_cls", BACKENDS, ids=lambda c: c.__name__)
def test_backend_close_keeps_shared_manager_swept(tmp_path: Path, backend_cls) -> None:
    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    shared = DatabaseConnectionManager.get_instance(db_path)
    try:
        backend = backend_cls(db_path)
        assert backend._conn_manager is shared
        backend.close()

        opened: List[sqlite3.Connection] = []
        worker = threading.Thread(target=lambda: opened.append(shared.get_connection()))
        worker.start()
        worker.join(10.0)
        assert not worker.is_alive() and len(opened) == 1

        DatabaseConnectionManager._sweep_all_instances_unthrottled()

        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
    finally:
        shared.close_all()
