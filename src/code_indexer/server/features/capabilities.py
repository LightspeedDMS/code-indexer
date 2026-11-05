"""The core's implementation of every ``cidx_sdk`` capability.

:func:`build_core_capabilities` returns, for each name in
``cidx_sdk.KNOWN_CAPABILITIES``, a provider: ``provider(feature_name)``
builds the capability object handed to that one feature through its
``FeatureContext`` (``registry.CoreFeatureContext`` calls it on first use).

Every adapter is a thin layer over the service the core already uses for the
same job, and reads it from ``app.state`` (or the service's own accessor)
when CALLED, never when built: features are loaded while the app is being
created, before the lifespan has started most services, and a feature asks
for a capability only once they are up (``on_startup``, a hook, a request).
A service that is not running raises ``RuntimeError`` naming it.

State rules (CLAUDE.md "Cluster-Aware State"): payload-cache keys and job
dedup keys are namespaced by feature and live in the cluster stores; the
only per-process state here is the health capability's published reports,
which describe THIS node like every other health input.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from cidx_sdk import HealthReport
from cidx_sdk.capabilities import (
    ACCESS_FILTER,
    ACCESS_PROJECTION,
    AUDIT_LOG,
    BACKGROUND_JOBS,
    CONFIG,
    CONTENT_SEAM,
    HEALTH,
    HTTP_CLIENT,
    PAYLOAD_CACHE,
    PERMISSIONS,
    READ_SCOPE,
    REGEX_TARGET,
    REPOSITORY_EVENTS,
    SEARCH_EVENTS,
    SNAPSHOT_PREDICATE,
    STORAGE,
    TELEMETRY,
)
from cidx_sdk.capability_types import (
    STORAGE_POSTGRES,
    STORAGE_SQLITE,
    ApplicationMetrics,
    Meter,
)

from code_indexer.kernel.seam_types import (
    ContentEntry,
    ContentSeamSource,
    RipgrepBackend,
)
from code_indexer.server.features import config as feature_config

logger = logging.getLogger(__name__)

#: ``provider(feature_name) -> capability object``.
CapabilityProvider = Callable[[str], object]

#: Prefix of every payload-cache key and job dedup key a feature writes.
FEATURE_KEY_PREFIX = "feature"


def feature_key(feature_name: str, key: str) -> str:
    """*key* namespaced to *feature_name* in a cluster-shared store."""
    return f"{FEATURE_KEY_PREFIX}:{feature_name}:{key}"


class JobAlreadyActiveError(RuntimeError):
    """The deduplicated job is already active somewhere in the cluster."""


def _state(app: Any, attr: str, what: str) -> Any:
    value = getattr(getattr(app, "state", None), attr, None)
    if value is None:
        raise RuntimeError(f"{what} is not running in this process")
    return value


def _registry(app: Any) -> Any:
    return _state(app, "feature_registry", "the feature registry")


# ── config ───────────────────────────────────────────────────────────────────


class CoreConfigCapability:
    """``config``: the config service, plus the feature's own section."""

    def __init__(self, app: Any, feature_name: str) -> None:
        self._app = app
        self._feature = feature_name

    def runtime_config(self) -> object:
        from code_indexer.server.services.config_service import get_config_service

        return get_config_service().get_config()

    def section(self) -> Mapping[str, object]:
        section = _registry(self._app).config_section_of(self._feature)
        if section is None:
            return {}
        return feature_config.section_values(
            self._feature, section, self.runtime_config()
        )

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        from code_indexer.server.services.config_service import get_config_service

        def _on_commit(before: object, after: object) -> None:
            callback()

        unregister: Callable[[], None] = (
            get_config_service().register_on_commit_callback(_on_commit)
        )
        return unregister


# ── payload cache ────────────────────────────────────────────────────────────


class CorePayloadCacheCapability:
    """``payload_cache``: ``app.state.payload_cache`` (SQLite solo, PG cluster)."""

    def __init__(self, app: Any, feature_name: str) -> None:
        self._app = app
        self._feature = feature_name

    def _cache(self) -> Any:
        return _state(self._app, "payload_cache", "the payload cache")

    def store(self, content: str, *, owner: Optional[str] = None) -> str:
        handle: str = self._cache().store(content, owner=owner)
        return handle

    def retrieve(self, handle: str) -> Optional[str]:
        from code_indexer.server.cache.payload_cache import CacheNotFoundError

        try:
            content: str = self._cache().retrieve_full(handle)
        except CacheNotFoundError:
            return None
        return content

    def store_with_key(self, key: str, content: str) -> None:
        self._cache().store_with_key(feature_key(self._feature, key), content)

    def retrieve_key(self, key: str) -> Optional[str]:
        return self.retrieve(feature_key(self._feature, key))

    def has_key(self, key: str) -> bool:
        return bool(self._cache().has_key(feature_key(self._feature, key)))


# ── background jobs ──────────────────────────────────────────────────────────


class CoreBackgroundJobsCapability:
    """``background_jobs``: BackgroundJobManager submissions, deduplicated
    across the cluster by the job tracker's active-job index when a
    ``dedup_key`` is given (a feature-namespaced sentinel alias)."""

    def __init__(self, app: Any, feature_name: str) -> None:
        self._app = app
        self._feature = feature_name

    def _manager(self) -> Any:
        return _state(self._app, "background_job_manager", "the job manager")

    def submit(
        self,
        operation_type: str,
        fn: Callable[[], Mapping[str, object]],
        *,
        submitter: str,
        dedup_key: Optional[str] = None,
    ) -> str:
        from code_indexer.server.repositories.background_jobs import (
            DuplicateJobError,
        )

        def _run() -> Dict[str, Any]:
            return dict(fn())

        alias = None if dedup_key is None else feature_key(self._feature, dedup_key)
        try:
            job_id: str = self._manager().submit_job(
                operation_type,
                _run,
                submitter_username=submitter,
                repo_alias=alias,
            )
        except DuplicateJobError as exc:
            raise JobAlreadyActiveError(
                f"{operation_type} for {alias} is already running in the cluster"
            ) from exc
        return job_id

    def run_registered(self, job_name: str, *, submitter: str) -> str:
        job, ctx = _registry(self._app).background_job_of(self._feature, job_name)
        feature = self._feature

        def _run() -> Mapping[str, object]:
            job.run(ctx)
            return {"feature": feature, "job": job.name}

        return self.submit(
            job.operation_type, _run, submitter=submitter, dedup_key=job.name
        )

    def status(self, job_id: str, *, username: str) -> Optional[Mapping[str, object]]:
        status: Optional[Mapping[str, object]] = self._manager().get_job_status(
            job_id, username
        )
        return status


# ── telemetry, HTTP clients ──────────────────────────────────────────────────


class CoreTelemetryCapability:
    """``telemetry``: the same adapter X-Ray and SCIP use (kernel_wiring)."""

    def __init__(self) -> None:
        from code_indexer.server.kernel_wiring import ServerTelemetryHost

        self._host = ServerTelemetryHost()

    def application_metrics(self) -> Optional[ApplicationMetrics]:
        return self._host.application_metrics()

    def application_meter(self) -> Optional[Meter]:
        return self._host.application_meter()

    def span(
        self, name: str, attributes: Optional[Dict[str, Any]] = None
    ) -> AbstractContextManager[Any]:
        return self._host.span(name, attributes)


class CoreHttpClientCapability:
    """``http_client``: the app's HttpClientFactory (fault injection aware)."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def _factory(self) -> Any:
        factory = getattr(
            getattr(self._app, "state", None), "http_client_factory", None
        )
        if factory is None:
            from code_indexer.server.fault_injection.http_client_factory import (
                HttpClientFactory,
            )

            factory = HttpClientFactory(fault_injection_service=None)
        return factory

    def sync_client(self, **kwargs: Any) -> Any:
        return self._factory().create_sync_client(**kwargs)

    def async_client(self, **kwargs: Any) -> Any:
        return self._factory().create_client(**kwargs)


# ── storage ──────────────────────────────────────────────────────────────────


class CoreStorageCapability:
    """``storage``: the PostgreSQL pool in cluster mode, the server SQLite
    database solo -- the same database the feature's migrations ran on."""

    def __init__(self, mode: str, pool: Any, sqlite_path: Optional[str]) -> None:
        self._mode = mode
        self._pool = pool
        self._sqlite_path = sqlite_path

    @property
    def mode(self) -> str:
        return self._mode

    def connection(self) -> AbstractContextManager[Any]:
        if self._mode == STORAGE_POSTGRES:
            # psycopg_pool commits on a normal exit and rolls back on error.
            cm: AbstractContextManager[Any] = self._pool.connection()
            return cm
        return self._sqlite_connection()

    @contextmanager
    def _sqlite_connection(self) -> Iterator[sqlite3.Connection]:
        if self._sqlite_path is None:
            raise RuntimeError("the server database path is not known")
        conn = sqlite3.connect(self._sqlite_path, timeout=30.0)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def storage_from_services(services: Mapping[str, Any]) -> CoreStorageCapability:
    """The storage capability for the backend ``initialize_services`` chose."""
    registry = services.get("backend_registry")
    pool = getattr(registry, "connection_pool", None)
    if services.get("storage_mode") == STORAGE_POSTGRES and pool is not None:
        return CoreStorageCapability(STORAGE_POSTGRES, pool, None)
    return CoreStorageCapability(STORAGE_SQLITE, None, services.get("db_path_str"))


# ── auth ─────────────────────────────────────────────────────────────────────


class CorePermissionsCapability:
    """``permissions``: the live account's role permissions (User model)."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def _user(self, username: str) -> Any:
        return _state(self._app, "user_manager", "the user manager").get_user(username)

    def has_permission(self, username: str, permission: str) -> bool:
        user = self._user(username)
        return user is not None and bool(user.has_permission(permission))

    def is_admin(self, username: str) -> bool:
        from code_indexer.server.auth.user_manager import UserRole

        user = self._user(username)
        return user is not None and user.role == UserRole.ADMIN


class CoreAccessFilterCapability:
    """``access_filter``: AccessFilteringService, the same decision as the
    MCP dispatcher's repository pre-check."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def _service(self) -> Any:
        return _state(
            self._app, "access_filtering_service", "the access filtering service"
        )

    def accessible_repositories(self, username: str) -> FrozenSet[str]:
        return frozenset(self._service().get_accessible_repos(username))

    def can_access_repository(self, username: str, repo_alias: str) -> bool:
        service = self._service()
        if service.is_admin_user(username):
            return True
        return bool(
            service.alias_granted(
                repo_alias,
                set(service.get_accessible_repos(username)),
                service.caller_activation_sources(username),
            )
        )


# ── audit log, search events ─────────────────────────────────────────────────


class CoreAuditLogCapability:
    """``audit_log``: ``audit_capture.capture`` against the core catalog."""

    def record(
        self,
        *,
        actor: str,
        action_type: str,
        target_type: str,
        target_id: str,
        outcome: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        from code_indexer.server.services import audit_capture

        audit_capture.capture(
            actor=actor,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            details=details,
        )


class CoreSearchEventsCapability:
    """``search_events``: the app's SearchEventLogWriter queue."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def record(
        self,
        *,
        tool: str,
        door: str,
        username: str,
        repo_alias: Optional[str],
        search_type: str,
        query_text: str,
        result_count: int,
        latency_ms: int,
        error_class: Optional[str] = None,
    ) -> None:
        from code_indexer.server.middleware.correlation import get_correlation_id
        from code_indexer.server.services.search_event_recording import (
            SearchOrigin,
            record_search_event,
            writer_from_app,
        )

        record_search_event(
            writer_from_app(self._app),
            origin=SearchOrigin(tool=tool, door=door),
            username=username,
            repo_alias=repo_alias,
            search_type=search_type,
            query_text=query_text,
            result_count=result_count,
            total_latency_ms=latency_ms,
            correlation_id=get_correlation_id(),
            error_class=error_class,
        )


# ── health ───────────────────────────────────────────────────────────────────


class CoreHealthCapability:
    """``health``: reports kept by the registry, listed in the health block."""

    def __init__(self, app: Any, feature_name: str) -> None:
        self._app = app
        self._feature = feature_name

    def publish(self, report: HealthReport) -> None:
        if not isinstance(report, HealthReport):
            raise TypeError("publish() takes a cidx_sdk HealthReport")
        _registry(self._app).publish_report(self._feature, report)

    def withdraw(self, component: str) -> None:
        _registry(self._app).withdraw_report(self._feature, component)


# ── kernel seams and path policy ─────────────────────────────────────────────


class CoreContentSeamCapability:
    """``content_seam``: the server's content backend wiring."""

    def __init__(self) -> None:
        from code_indexer.server.kernel_wiring import ServerContentSeamHost

        self._host = ServerContentSeamHost()

    def is_delegated(self) -> bool:
        return self._host.is_delegated()

    def source_for(
        self, repo_path: str, *, consumer: str
    ) -> Optional[ContentSeamSource]:
        return self._host.source_for(repo_path, consumer=consumer)

    def iter_manifest_entries(
        self,
        source: ContentSeamSource,
        prune_dirs: Sequence[str],
        *,
        deadline: Optional[float],
    ) -> Iterator[ContentEntry]:
        return self._host.iter_manifest_entries(source, prune_dirs, deadline=deadline)


class CoreRegexTargetCapability:
    """``regex_target``: the server's ripgrep backend wiring."""

    def regex_target(self, repo_path: Path) -> Tuple[Path, Optional[RipgrepBackend]]:
        from code_indexer.server.kernel_wiring import ServerRegexTargetHost

        return ServerRegexTargetHost().regex_target(repo_path)


class CoreSnapshotPredicateCapability:
    """``snapshot_predicate``: ``code_indexer.utils.snapshot_paths``."""

    def is_versioned_snapshot(self, path: str) -> bool:
        from code_indexer.utils.snapshot_paths import is_versioned_snapshot

        return is_versioned_snapshot(path)

    def is_immutable_versioned_snapshot(self, path: str) -> bool:
        from code_indexer.utils.snapshot_paths import (
            is_immutable_versioned_snapshot,
        )

        return is_immutable_versioned_snapshot(path)


class CoreReadScopeCapability:
    """``read_scope``: the served-index read scope carrier."""

    def carry(self, fn: Callable[[], None]) -> Callable[[], None]:
        from code_indexer.server.kernel_wiring import ServerReadScopeHost

        return ServerReadScopeHost().carry(fn)


def build_core_capabilities(
    app: Any, services: Mapping[str, Any]
) -> Dict[str, CapabilityProvider]:
    """One provider per ``cidx_sdk`` capability name, over *app*'s services.

    *services* is ``initialize_services()``'s result; only the storage
    selection is read from it (the rest is resolved from ``app.state`` on
    use). Cheap: builds closures, imports and touches no service.
    """
    storage = storage_from_services(services)
    return {
        CONFIG: lambda name: CoreConfigCapability(app, name),
        PAYLOAD_CACHE: lambda name: CorePayloadCacheCapability(app, name),
        BACKGROUND_JOBS: lambda name: CoreBackgroundJobsCapability(app, name),
        TELEMETRY: lambda name: CoreTelemetryCapability(),
        HTTP_CLIENT: lambda name: CoreHttpClientCapability(app),
        STORAGE: lambda name: storage,
        PERMISSIONS: lambda name: CorePermissionsCapability(app),
        ACCESS_FILTER: lambda name: CoreAccessFilterCapability(app),
        AUDIT_LOG: lambda name: CoreAuditLogCapability(),
        SEARCH_EVENTS: lambda name: CoreSearchEventsCapability(app),
        HEALTH: lambda name: CoreHealthCapability(app, name),
        CONTENT_SEAM: lambda name: CoreContentSeamCapability(),
        REGEX_TARGET: lambda name: CoreRegexTargetCapability(),
        SNAPSHOT_PREDICATE: lambda name: CoreSnapshotPredicateCapability(),
        READ_SCOPE: lambda name: CoreReadScopeCapability(),
        ACCESS_PROJECTION: lambda name: _access_projection(app),
        REPOSITORY_EVENTS: lambda name: _repository_events(),
    }


def _access_projection(app: Any) -> Any:
    from code_indexer.server.features.access_projection import (
        CoreAccessProjectionCapability,
    )

    return CoreAccessProjectionCapability(app)


def _repository_events() -> Any:
    from code_indexer.server.features.repository_events import (
        CoreRepositoryEventsCapability,
    )

    return CoreRepositoryEventsCapability()
