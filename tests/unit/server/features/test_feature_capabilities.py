"""The core's capability providers conform to the SDK Protocols and use the
core services (modular phase 4a)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional

import pytest
from cidx_sdk import (
    CAPABILITY_PROTOCOLS,
    KNOWN_CAPABILITIES,
    BackgroundJob,
    BaseFeature,
    FeatureManifest,
    HealthReport,
)
from cidx_sdk.capabilities import BACKGROUND_JOBS, HEALTH, PAYLOAD_CACHE, STORAGE
from cidx_sdk.capability_types import STORAGE_POSTGRES, STORAGE_SQLITE

from code_indexer.server.features.capabilities import (
    CoreBackgroundJobsCapability,
    CorePayloadCacheCapability,
    CorePermissionsCapability,
    CoreStorageCapability,
    JobAlreadyActiveError,
    build_core_capabilities,
    feature_key,
    storage_from_services,
)
from code_indexer.server.features.loader import FeatureCandidate
from code_indexer.server.features.registry import FeatureRegistry, degraded_reasons
from code_indexer.server.startup.feature_wiring import build_feature_registry


def _app(**state: Any) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(**state))


def test_every_known_capability_is_provided_and_conforms(tmp_path: Path) -> None:
    providers = build_core_capabilities(
        _app(), {"storage_mode": "sqlite", "db_path_str": str(tmp_path / "db")}
    )
    assert set(providers) == set(KNOWN_CAPABILITIES)
    for name, provider in providers.items():
        assert isinstance(provider("example"), CAPABILITY_PROTOCOLS[name]), name


class _Cache:
    def __init__(self) -> None:
        self.rows: Dict[str, str] = {}

    def store(self, content: str, *, owner: Optional[str] = None) -> str:
        self.rows["h1"] = content
        return "h1"

    def store_with_key(self, key: str, content: str) -> None:
        self.rows[key] = content

    def has_key(self, key: str) -> bool:
        return key in self.rows

    def retrieve_full(self, handle: str) -> str:
        from code_indexer.server.cache.payload_cache import CacheNotFoundError

        if handle not in self.rows:
            raise CacheNotFoundError(handle)
        return self.rows[handle]


def test_payload_cache_keys_are_namespaced_per_feature() -> None:
    cache = _Cache()
    cap = CorePayloadCacheCapability(_app(payload_cache=cache), "example")
    cap.store_with_key("k", "v")
    assert cache.rows == {feature_key("example", "k"): "v"}
    assert cap.has_key("k") and cap.retrieve_key("k") == "v"
    assert cap.retrieve("missing") is None
    assert cap.retrieve(cap.store("x")) == "x"
    other = CorePayloadCacheCapability(_app(payload_cache=cache), "other")
    assert not other.has_key("k")


def test_a_service_that_is_not_running_is_named() -> None:
    cap = CorePayloadCacheCapability(_app(), "example")
    with pytest.raises(RuntimeError, match="payload cache"):
        cap.has_key("k")


def test_storage_sqlite_commits_and_rolls_back(tmp_path: Path) -> None:
    db = str(tmp_path / "server.db")
    storage = storage_from_services({"storage_mode": "sqlite", "db_path_str": db})
    assert storage.mode == STORAGE_SQLITE
    with storage.connection() as conn:
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(ValueError):
        with storage.connection() as conn:
            conn.execute("INSERT INTO t VALUES (2)")
            raise ValueError("abort")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT v FROM t").fetchall() == [(1,)]


def test_storage_postgres_uses_the_pool() -> None:
    pool = SimpleNamespace(connection=lambda: "pooled-connection-cm")
    storage = storage_from_services(
        {
            "storage_mode": "postgres",
            "backend_registry": SimpleNamespace(connection_pool=pool),
        }
    )
    assert storage.mode == STORAGE_POSTGRES
    assert storage.connection() == "pooled-connection-cm"
    assert isinstance(storage, CoreStorageCapability)


def test_permissions_use_the_live_account() -> None:
    admin = SimpleNamespace(role="admin", has_permission=lambda p: True)
    accounts = SimpleNamespace(get_user=lambda name: admin if name == "a" else None)
    cap = CorePermissionsCapability(_app(user_manager=accounts))
    assert cap.has_permission("a", "manage_users")
    assert not cap.has_permission("ghost", "query_repos")
    assert not cap.is_admin("ghost")


class _Manager:
    def __init__(self, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.calls: List[Dict[str, Any]] = []

    def submit_job(self, operation_type: str, func: Any, **kwargs: Any) -> str:
        from code_indexer.server.repositories.background_jobs import (
            DuplicateJobError,
        )

        if self.duplicate:
            raise DuplicateJobError(operation_type, kwargs["repo_alias"], "j0")
        self.calls.append(
            {"operation_type": operation_type, "result": func(), **kwargs}
        )
        return "j1"

    def get_job_status(self, job_id: str, username: str) -> Mapping[str, Any]:
        return {"job_id": job_id, "username": username}


def test_background_jobs_dedup_key_is_namespaced_and_duplicates_raise() -> None:
    manager = _Manager()
    cap = CoreBackgroundJobsCapability(_app(background_job_manager=manager), "ex")
    assert cap.submit("ex_op", lambda: {"a": 1}, submitter="u", dedup_key="d") == "j1"
    (call,) = manager.calls
    assert call["repo_alias"] == feature_key("ex", "d")
    assert call["result"] == {"a": 1} and call["submitter_username"] == "u"
    assert cap.status("j1", username="u") == {"job_id": "j1", "username": "u"}
    busy = CoreBackgroundJobsCapability(
        _app(background_job_manager=_Manager(duplicate=True)), "ex"
    )
    with pytest.raises(JobAlreadyActiveError):
        busy.submit("ex_op", lambda: {}, submitter="u", dedup_key="d")


class _JobFeature(BaseFeature):
    def __init__(self) -> None:
        super().__init__(
            FeatureManifest(name="jobs", version="1.0.0", requires_sdk=">=0.1")
        )
        self.runs: List[str] = []

    def background_jobs(self) -> Any:
        return (
            BackgroundJob(
                "sweep", "jobs_sweep", lambda ctx: self.runs.append(ctx.feature_name)
            ),
        )


def _registry_with(app: Any, feature: BaseFeature) -> FeatureRegistry:
    candidate = FeatureCandidate(
        name=feature.manifest.name, load=lambda: lambda: feature, source="inline"
    )
    providers = build_core_capabilities(app, {"storage_mode": "sqlite"})
    registry = build_feature_registry(
        SimpleNamespace(enabled_features=[feature.manifest.name]),
        discover=lambda: {feature.manifest.name: candidate},
        capabilities=providers,
    )
    app.state.feature_registry = registry
    return registry


def test_run_registered_runs_the_feature_job_with_its_context() -> None:
    manager = _Manager()
    app = _app(background_job_manager=manager)
    feature = _JobFeature()
    registry = _registry_with(app, feature)
    jobs: Any = registry.context_for("jobs").capability(BACKGROUND_JOBS)
    assert jobs.run_registered("sweep", submitter="system") == "j1"
    assert feature.runs == ["jobs"]
    assert manager.calls[0]["result"] == {"feature": "jobs", "job": "sweep"}
    with pytest.raises(KeyError):
        jobs.run_registered("absent", submitter="system")


def test_context_reuses_one_capability_object() -> None:
    app = _app(payload_cache=_Cache())
    registry = _registry_with(app, _JobFeature())
    ctx = registry.context_for("jobs")
    assert ctx.capability(PAYLOAD_CACHE) is ctx.capability(PAYLOAD_CACHE)
    assert registry.context_for("jobs") is ctx


def test_health_capability_reports_join_the_health_block_and_degrade() -> None:
    app = _app()
    registry = _registry_with(app, _JobFeature())
    health: Any = registry.context_for("jobs").capability(HEALTH)
    health.publish(HealthReport("jobs-queue", "unhealthy", reasons=("stuck",)))
    payload = registry.health_payload()
    reports = payload["loaded"][0]["health"]
    assert [r["component"] for r in reports] == ["jobs-queue"]
    assert degraded_reasons(payload) == ["Feature jobs (jobs-queue): stuck"]
    health.withdraw("jobs-queue")
    assert degraded_reasons(registry.health_payload()) == []
    with pytest.raises(TypeError):
        health.publish("not a report")


def test_degraded_reasons_without_a_registry() -> None:
    assert degraded_reasons(None) == []


def test_storage_capability_from_the_registry(tmp_path: Path) -> None:
    app = _app()
    registry = _registry_with(app, _JobFeature())
    storage: Any = registry.context_for("jobs").capability(STORAGE)
    assert storage.mode == STORAGE_SQLITE
