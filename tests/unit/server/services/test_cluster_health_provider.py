"""
Unit tests for ClusterHealthProvider.

Story #430: Health Endpoint Cluster Awareness.

All dependencies are exercised via lightweight stub objects so that tests
run without any external infrastructure (no PostgreSQL, no NFS, no etcd).
"""

import time

from code_indexer.server.services.cluster_health_provider import ClusterHealthProvider


# ---------------------------------------------------------------------------
# Stub helpers (prefer stubs over Mock to avoid "mocks are lies")
# ---------------------------------------------------------------------------


class _StubLeaderElection:
    """Minimal leader-election stub."""

    def __init__(self, is_leader: bool = True, is_active: bool = True):
        self._is_leader = is_leader
        self._is_active = is_active

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    def is_active(self) -> bool:
        return self._is_active


class _StubLeaderElectionNoActive:
    """Leader election stub that does NOT expose is_active (optional method)."""

    def __init__(self, is_leader: bool = True):
        self._is_leader = is_leader

    @property
    def is_leader(self) -> bool:
        return self._is_leader


class _StubNfsValidator:
    """Minimal NFS mount validator stub."""

    def __init__(self, path: str = "/mnt/cidx-shared", mounted: bool = True):
        self._path = path
        self._mounted = mounted

    def get_mount_path(self) -> str:
        return self._path

    def is_mounted(self) -> bool:
        return self._mounted


class _StubCursor:
    def execute(self, _sql):
        pass

    def fetchone(self):
        return (1,)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _StubConn:
    def cursor(self):
        return _StubCursor()


class _StubPgPool:
    """Minimal synchronous PostgreSQL pool stub (psycopg v3 style)."""

    def __init__(self, should_fail: bool = False):
        self._should_fail = should_fail

    class _ConnectionCtx:
        def __init__(self, should_fail):
            self._should_fail = should_fail

        def __enter__(self):
            if self._should_fail:
                raise RuntimeError("connection refused")
            return _StubConn()

        def __exit__(self, *args):
            pass

    def connection(self):
        return self._ConnectionCtx(self._should_fail)


class _RaisingLeaderElection:
    """Leader election that raises on every access."""

    @property
    def is_leader(self) -> bool:
        raise RuntimeError("etcd unavailable")

    def is_active(self) -> bool:
        raise RuntimeError("etcd unavailable")


class _RaisingNfsValidator:
    """NFS validator that raises on every call."""

    def get_mount_path(self) -> str:
        raise OSError("stat failed")

    def is_mounted(self) -> bool:
        raise OSError("stat failed")


# ---------------------------------------------------------------------------
# Test: cluster health structure
# ---------------------------------------------------------------------------


class TestClusterHealthStructure:
    def test_returns_cluster_mode_true(self):
        provider = ClusterHealthProvider("cidx-node-01")
        result = provider.get_cluster_health()
        assert result["cluster_mode"] is True

    def test_contains_node_section(self):
        provider = ClusterHealthProvider("cidx-node-01")
        result = provider.get_cluster_health()
        assert "node" in result

    def test_contains_checks_section(self):
        provider = ClusterHealthProvider("cidx-node-01")
        result = provider.get_cluster_health()
        assert "checks" in result

    def test_checks_has_all_three_keys(self):
        provider = ClusterHealthProvider("cidx-node-01")
        checks = provider.get_cluster_health()["checks"]
        assert "postgresql" in checks
        assert "nfs_mount" in checks
        assert "leader_election" in checks


# ---------------------------------------------------------------------------
# Test: node section fields
# ---------------------------------------------------------------------------


class TestNodeSection:
    def test_node_id_matches_constructor(self):
        provider = ClusterHealthProvider("cidx-node-42")
        node = provider.get_cluster_health()["node"]
        assert node["node_id"] == "cidx-node-42"

    def test_storage_mode_is_postgres(self):
        provider = ClusterHealthProvider("n1")
        node = provider.get_cluster_health()["node"]
        assert node["storage_mode"] == "postgres"

    def test_uptime_seconds_is_non_negative_int(self):
        provider = ClusterHealthProvider("n1")
        node = provider.get_cluster_health()["node"]
        assert isinstance(node["uptime_seconds"], int)
        assert node["uptime_seconds"] >= 0

    def test_uptime_increases_over_time(self):
        provider = ClusterHealthProvider("n1")
        # Force start_time slightly in the past
        provider._start_time = time.time() - 5
        node = provider.get_cluster_health()["node"]
        assert node["uptime_seconds"] >= 5

    def test_role_leader_when_leader_election_says_leader(self):
        provider = ClusterHealthProvider(
            "n1", leader_election_service=_StubLeaderElection(is_leader=True)
        )
        node = provider.get_cluster_health()["node"]
        assert node["role"] == "leader"

    def test_role_follower_when_leader_election_says_follower(self):
        provider = ClusterHealthProvider(
            "n1", leader_election_service=_StubLeaderElection(is_leader=False)
        )
        node = provider.get_cluster_health()["node"]
        assert node["role"] == "follower"

    def test_role_unknown_when_no_leader_election_service(self):
        provider = ClusterHealthProvider("n1")
        node = provider.get_cluster_health()["node"]
        assert node["role"] == "unknown"

    def test_role_unknown_when_leader_election_raises(self):
        provider = ClusterHealthProvider(
            "n1", leader_election_service=_RaisingLeaderElection()
        )
        node = provider.get_cluster_health()["node"]
        assert node["role"] == "unknown"


# ---------------------------------------------------------------------------
# Test: PostgreSQL check
# ---------------------------------------------------------------------------


class TestPostgresqlCheck:
    def test_healthy_when_pool_succeeds(self):
        provider = ClusterHealthProvider("n1", pg_pool=_StubPgPool())
        result = provider.get_cluster_health()["checks"]["postgresql"]
        assert result["status"] == "healthy"

    def test_latency_ms_is_non_negative_int_when_healthy(self):
        provider = ClusterHealthProvider("n1", pg_pool=_StubPgPool())
        result = provider.get_cluster_health()["checks"]["postgresql"]
        assert isinstance(result["latency_ms"], int)
        assert result["latency_ms"] >= 0

    def test_unavailable_when_no_pool(self):
        provider = ClusterHealthProvider("n1")
        result = provider.get_cluster_health()["checks"]["postgresql"]
        assert result["status"] == "unavailable"
        assert result["latency_ms"] is None

    def test_unhealthy_when_pool_raises(self):
        provider = ClusterHealthProvider("n1", pg_pool=_StubPgPool(should_fail=True))
        result = provider.get_cluster_health()["checks"]["postgresql"]
        assert result["status"] == "unhealthy"

    def test_error_message_present_when_pool_raises(self):
        provider = ClusterHealthProvider("n1", pg_pool=_StubPgPool(should_fail=True))
        result = provider.get_cluster_health()["checks"]["postgresql"]
        assert "error" in result
        assert len(result["error"]) > 0


# ---------------------------------------------------------------------------
# Test: NFS mount check
# ---------------------------------------------------------------------------


class TestNfsMountCheck:
    def test_healthy_when_mounted(self):
        provider = ClusterHealthProvider(
            "n1", nfs_validator=_StubNfsValidator(mounted=True)
        )
        result = provider.get_cluster_health()["checks"]["nfs_mount"]
        assert result["status"] == "healthy"

    def test_path_included_when_mounted(self):
        provider = ClusterHealthProvider(
            "n1",
            nfs_validator=_StubNfsValidator(path="/mnt/cidx-shared", mounted=True),
        )
        result = provider.get_cluster_health()["checks"]["nfs_mount"]
        assert result["path"] == "/mnt/cidx-shared"

    def test_unhealthy_when_not_mounted(self):
        provider = ClusterHealthProvider(
            "n1", nfs_validator=_StubNfsValidator(mounted=False)
        )
        result = provider.get_cluster_health()["checks"]["nfs_mount"]
        assert result["status"] == "unhealthy"

    def test_unavailable_when_no_validator(self):
        provider = ClusterHealthProvider("n1")
        result = provider.get_cluster_health()["checks"]["nfs_mount"]
        assert result["status"] == "unavailable"
        assert result["path"] is None

    def test_unhealthy_when_validator_raises(self):
        provider = ClusterHealthProvider("n1", nfs_validator=_RaisingNfsValidator())
        result = provider.get_cluster_health()["checks"]["nfs_mount"]
        assert result["status"] == "unhealthy"


# ---------------------------------------------------------------------------
# Test: leader election check
# ---------------------------------------------------------------------------


class TestLeaderElectionCheck:
    def test_active_and_is_leader_true_when_leader(self):
        provider = ClusterHealthProvider(
            "n1",
            leader_election_service=_StubLeaderElection(is_leader=True, is_active=True),
        )
        result = provider.get_cluster_health()["checks"]["leader_election"]
        assert result["status"] == "active"
        assert result["is_leader"] is True

    def test_inactive_when_is_active_false(self):
        provider = ClusterHealthProvider(
            "n1",
            leader_election_service=_StubLeaderElection(
                is_leader=False, is_active=False
            ),
        )
        result = provider.get_cluster_health()["checks"]["leader_election"]
        assert result["status"] == "inactive"

    def test_works_without_is_active_method(self):
        """is_active() is optional; missing it must not crash."""
        provider = ClusterHealthProvider(
            "n1",
            leader_election_service=_StubLeaderElectionNoActive(is_leader=True),
        )
        result = provider.get_cluster_health()["checks"]["leader_election"]
        assert result["status"] == "active"
        assert result["is_leader"] is True

    def test_unavailable_when_no_service(self):
        provider = ClusterHealthProvider("n1")
        result = provider.get_cluster_health()["checks"]["leader_election"]
        assert result["status"] == "unavailable"
        assert result["is_leader"] is None

    def test_error_when_service_raises(self):
        provider = ClusterHealthProvider(
            "n1", leader_election_service=_RaisingLeaderElection()
        )
        result = provider.get_cluster_health()["checks"]["leader_election"]
        assert result["status"] == "error"
        assert result["is_leader"] is None


# ---------------------------------------------------------------------------
# Test: is_healthy
# ---------------------------------------------------------------------------


class TestIsHealthy:
    def test_true_when_pg_and_nfs_healthy(self):
        provider = ClusterHealthProvider(
            "n1",
            pg_pool=_StubPgPool(),
            nfs_validator=_StubNfsValidator(mounted=True),
        )
        assert provider.is_healthy() is True

    def test_false_when_pg_down(self):
        provider = ClusterHealthProvider(
            "n1",
            pg_pool=_StubPgPool(should_fail=True),
            nfs_validator=_StubNfsValidator(mounted=True),
        )
        assert provider.is_healthy() is False

    def test_false_when_nfs_not_mounted(self):
        provider = ClusterHealthProvider(
            "n1",
            pg_pool=_StubPgPool(),
            nfs_validator=_StubNfsValidator(mounted=False),
        )
        assert provider.is_healthy() is False

    def test_false_when_both_down(self):
        provider = ClusterHealthProvider(
            "n1",
            pg_pool=_StubPgPool(should_fail=True),
            nfs_validator=_StubNfsValidator(mounted=False),
        )
        assert provider.is_healthy() is False

    def test_false_when_no_dependencies(self):
        """With no pool or validator both checks are 'unavailable', not 'healthy'."""
        provider = ClusterHealthProvider("n1")
        assert provider.is_healthy() is False

    def test_leader_election_failure_does_not_affect_is_healthy(self):
        """Leader election is informational — its failure must not flip is_healthy."""
        provider = ClusterHealthProvider(
            "n1",
            pg_pool=_StubPgPool(),
            nfs_validator=_StubNfsValidator(mounted=True),
            leader_election_service=_RaisingLeaderElection(),
        )
        assert provider.is_healthy() is True


# ---------------------------------------------------------------------------
# Test: standalone mode
# ---------------------------------------------------------------------------


class TestStandaloneHealth:
    def test_returns_cluster_mode_false(self):
        provider = ClusterHealthProvider("n1")
        result = provider.get_standalone_health()
        assert result["cluster_mode"] is False

    def test_returns_sqlite_storage_mode(self):
        provider = ClusterHealthProvider("n1")
        result = provider.get_standalone_health()
        assert result["storage_mode"] == "sqlite"

    def test_does_not_contain_node_section(self):
        provider = ClusterHealthProvider("n1")
        result = provider.get_standalone_health()
        assert "node" not in result

    def test_does_not_contain_checks_section(self):
        provider = ClusterHealthProvider("n1")
        result = provider.get_standalone_health()
        assert "checks" not in result
