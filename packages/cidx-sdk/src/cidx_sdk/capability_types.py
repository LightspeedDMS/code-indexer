"""The type of every capability the core hands a feature.

One Protocol per name in :data:`cidx_sdk.capabilities.KNOWN_CAPABILITIES`
(:data:`CAPABILITY_PROTOCOLS` maps one to the other). A feature asks its
:class:`~cidx_sdk.capabilities.FeatureContext` for a capability by name and
narrows the result to the Protocol::

    config = cast(ConfigCapability, ctx.capability(CONFIG))
    values = config.section()

Rules every implementation follows (the core's conformance tests pin them):

* Blocking. Call from a worker thread, never on the event loop; the core
  already runs every hook and every MCP handler off the loop.
* Cluster-aware. Anything that must be visible to another request lives in
  a shared store (payload cache, job tracker, the database), never in a
  per-node dict.
* A capability object is bound to ONE feature (the context that returned
  it). Keys and job names it writes are namespaced by that feature.

Seam values (a content-seam source, a manifest entry, a ripgrep backend) are
the seam Protocols' business, not this module's: the seam-shaped
capabilities are generic over them, and the core binds the concrete seam
types.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    runtime_checkable,
)

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
from cidx_sdk.feature import HealthReport

# ── configuration ────────────────────────────────────────────────────────────


@runtime_checkable
class RuntimeConfigProvider(Protocol):
    """The server's runtime configuration (the config service, never env vars)."""

    def runtime_config(self) -> object:
        """The current configuration object; read its fields with getattr."""
        ...


@runtime_checkable
class ConfigCapability(RuntimeConfigProvider, Protocol):
    """``config``: the feature's own settings plus the server's configuration."""

    def section(self) -> Mapping[str, object]:
        """The feature's ``config_section()`` values: each key's default,
        overlaid by its bootstrap value (``config.json``) or its runtime value
        (the config service). Reads committed state; never cached across
        calls by the core."""
        ...

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call *callback* after this process commits a configuration change.
        Other processes see the change at their next read. Returns the handle
        that unsubscribes."""
        ...


# ── payload cache ────────────────────────────────────────────────────────────


@runtime_checkable
class PayloadCacheCapability(Protocol):
    """``payload_cache``: the cluster-shared, TTL-evicted payload store."""

    def store(self, content: str, *, owner: Optional[str] = None) -> str:
        """Store *content*; returns a new handle. *owner* restricts the
        generic retrieval doors to that user."""
        ...

    def retrieve(self, handle: str) -> Optional[str]:
        """The full content of *handle*, or None when unknown or expired."""
        ...

    def store_with_key(self, key: str, content: str) -> None:
        """Store *content* under the feature-namespaced *key* (replaces)."""
        ...

    def retrieve_key(self, key: str) -> Optional[str]:
        """The content stored under *key*, or None when unknown or expired."""
        ...

    def has_key(self, key: str) -> bool:
        """Whether *key* holds a live entry."""
        ...


# ── background jobs ──────────────────────────────────────────────────────────


@runtime_checkable
class BackgroundJobsCapability(Protocol):
    """``background_jobs``: the job manager and its cluster job tracker.

    Jobs are visible in the dashboard job list. A job given a ``dedup_key``
    runs at most once at a time across the cluster.
    """

    def submit(
        self,
        operation_type: str,
        fn: Callable[[], Mapping[str, object]],
        *,
        submitter: str,
        dedup_key: Optional[str] = None,
    ) -> str:
        """Run *fn* as a background job; returns its job id.

        Raises:
            RuntimeError: a job with the same *operation_type* and
                *dedup_key* is already active somewhere in the cluster.
        """
        ...

    def run_registered(self, job_name: str, *, submitter: str) -> str:
        """Run one of the feature's ``background_jobs()`` now; returns the
        job id. Raises ``KeyError`` for a name the feature did not register,
        ``RuntimeError`` when it is already running in the cluster."""
        ...

    def status(self, job_id: str, *, username: str) -> Optional[Mapping[str, object]]:
        """The job's status as the job list shows it, or None if unknown."""
        ...


# ── telemetry ────────────────────────────────────────────────────────────────

#: An OpenTelemetry attribute value, without importing OpenTelemetry.
AttributeValue = Union[
    str, bool, int, float, Sequence[str], Sequence[bool], Sequence[int], Sequence[float]
]


class MetricCounter(Protocol):
    """A monotonic counter instrument."""

    def add(
        self,
        amount: Union[int, float],
        attributes: Optional[Mapping[str, AttributeValue]] = None,
    ) -> None: ...


class Meter(Protocol):
    """Creates metric instruments."""

    def create_counter(
        self, name: str, unit: str = "", description: str = ""
    ) -> MetricCounter: ...


class ApplicationMetrics(Protocol):
    """The application metric recorders a library records through.

    Carries the one recorder the X-Ray package uses today; it moves into the
    X-Ray feature with it.
    """

    def record_xray_cache_identity_failure(self, reason: str) -> None: ...


@runtime_checkable
class TelemetryCapability(Protocol):
    """``telemetry``: metrics and spans. Never starts telemetry that is not
    already running; every method answers a no-op when it is off."""

    def application_metrics(self) -> Optional[ApplicationMetrics]:
        """The application metrics when telemetry is running and active."""
        ...

    def application_meter(self) -> Optional[Meter]:
        """The ``cidx.application`` meter when application metrics are active."""
        ...

    def span(
        self, name: str, attributes: Optional[Dict[str, Any]] = None
    ) -> AbstractContextManager[Any]:
        """A span around a block of work (a no-op span when tracing is off)."""
        ...


# ── HTTP client factory ──────────────────────────────────────────────────────


@runtime_checkable
class HttpClientCapability(Protocol):
    """``http_client``: outbound HTTP clients built the way the core builds
    its own (fault injection, connection pooling). The objects are
    ``httpx.Client`` / ``httpx.AsyncClient``; the SDK does not import httpx."""

    def sync_client(self, **kwargs: Any) -> Any:
        """A new ``httpx.Client``; the caller closes it."""
        ...

    def async_client(self, **kwargs: Any) -> Any:
        """A new ``httpx.AsyncClient``; the caller closes it."""
        ...


# ── storage ──────────────────────────────────────────────────────────────────

STORAGE_SQLITE = "sqlite"
STORAGE_POSTGRES = "postgres"


@runtime_checkable
class StorageCapability(Protocol):
    """``storage``: the active storage backend (SQLite solo, PostgreSQL
    cluster). Features keep their tables there, created by ``migrations()``."""

    @property
    def mode(self) -> str:
        """``STORAGE_SQLITE`` or ``STORAGE_POSTGRES``."""
        ...

    def connection(self) -> AbstractContextManager[Any]:
        """A DB-API connection to the server database: from the PostgreSQL
        pool in cluster mode, a new SQLite connection solo. Commits when the
        block ends normally, rolls back when it raises."""
        ...


# ── auth: permissions and repository access ──────────────────────────────────


@runtime_checkable
class PermissionsCapability(Protocol):
    """``permissions``: the role and permission model MCP tools are gated by."""

    def has_permission(self, username: str, permission: str) -> bool:
        """Whether the live account *username* holds *permission*; False for
        an unknown account."""
        ...

    def is_admin(self, username: str) -> bool:
        """Whether the live account *username* is an administrator."""
        ...


@runtime_checkable
class AccessFilterCapability(Protocol):
    """``access_filter``: which repositories a user may read (group grants)."""

    def accessible_repositories(self, username: str) -> FrozenSet[str]:
        """Golden repository names granted to *username*."""
        ...

    def can_access_repository(self, username: str, repo_alias: str) -> bool:
        """The same decision the MCP dispatcher's repository pre-check makes."""
        ...


# ── audit log and search events ──────────────────────────────────────────────


@runtime_checkable
class AuditLogCapability(Protocol):
    """``audit_log``: record an audit row through the core's catalog.

    The action type must be one the core's audit catalog knows, with the
    ``details`` fields that type allows; anything else is reported as a
    dropped record and never raises.
    """

    def record(
        self,
        *,
        actor: str,
        action_type: str,
        target_type: str,
        target_id: str,
        outcome: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None: ...


@runtime_checkable
class SearchEventsCapability(Protocol):
    """``search_events``: one row in the query analytics log. Never raises and
    never blocks; a recording failure is logged and dropped."""

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
    ) -> None: ...


# ── health ───────────────────────────────────────────────────────────────────


@runtime_checkable
class HealthCapability(Protocol):
    """``health``: push a health report between ``health()`` polls.

    Published reports are listed with the feature's ``health()`` reports.
    They describe THIS node (health is per node). A feature report that is
    not healthy makes the overall status degraded, never unhealthy.
    """

    def publish(self, report: HealthReport) -> None:
        """Set the current report for ``report.component`` (replaces)."""
        ...

    def withdraw(self, component: str) -> None:
        """Remove the report for *component*, if any."""
        ...


# ── kernel seams and path policy ─────────────────────────────────────────────

SourceT = TypeVar("SourceT")
EntryT_co = TypeVar("EntryT_co", covariant=True)
BackendT_co = TypeVar("BackendT_co", covariant=True)


@runtime_checkable
class ContentSeamCapability(Protocol[SourceT, EntryT_co]):
    """``content_seam``: reads a repository's files when they are not on this
    machine. Generic over the seam's source handle and manifest entry."""

    def is_delegated(self) -> bool:
        """True when repository files are read through a NON-local backend."""
        ...

    def source_for(self, repo_path: str, *, consumer: str) -> Optional[SourceT]:
        """The seam to read *repo_path* through, or None to read the local
        disk. *consumer* names the caller in any warning."""
        ...

    def iter_manifest_entries(
        self,
        source: SourceT,
        prune_dirs: Sequence[str],
        *,
        deadline: Optional[float],
    ) -> Iterator[EntryT_co]:
        """Every manifest entry of *source* outside *prune_dirs*, page by page;
        raises the seam's deadline error once the monotonic *deadline*
        passes."""
        ...


@runtime_checkable
class RegexTargetCapability(Protocol[BackendT_co]):
    """``regex_target``: where a regex search over a repository runs."""

    def regex_target(self, repo_path: Path) -> Tuple[Path, Optional[BackendT_co]]:
        """The root ripgrep searches for *repo_path*, and the backend that runs
        it (None: ripgrep in this process)."""
        ...


@runtime_checkable
class SnapshotPredicateCapability(Protocol):
    """``snapshot_predicate``: the canonical versioned-snapshot path rules."""

    def is_versioned_snapshot(self, path: str) -> bool:
        """Whether *path* is at or inside a versioned snapshot."""
        ...

    def is_immutable_versioned_snapshot(self, path: str) -> bool:
        """Whether *path* is at or inside an IMMUTABLE canonical snapshot
        (safe to cache without a TTL)."""
        ...


@runtime_checkable
class ReadScopeCapability(Protocol):
    """``read_scope``: keeps a read's served index checkouts held while
    another thread uses them."""

    def carry(self, fn: Callable[[], None]) -> Callable[[], None]:
        """*fn* bound to the checkouts the running read holds NOW. Call inside
        the read, before handing *fn* to another thread."""
        ...


# ── catalog access projection (0.5) ──────────────────────────────────────────


@runtime_checkable
class CatalogArticleView(Protocol):
    """One caller's restricted view of the internal catalog repository, for a
    feature that serves the catalog's files as articles (the wiki).

    Paths are catalog-relative POSIX paths. A path the caller may not see is
    answered exactly like a missing file: every method returns ``None`` (or
    drops the entry), never an error that tells the two apart. Nothing
    caller-specific may be written to a shared cache: a feature renders what
    :meth:`project_article` returns per request.
    """

    def served_path(
        self, requested: str, *, md_fallback: bool = False
    ) -> Optional[str]:
        """The catalog-relative path to serve for *requested* from the local
        tree, or None (missing, not the canonical spelling, escaping the
        repository, or denied). With *md_fallback* an extension-less article
        name is its ``.md`` file when that exists."""
        ...

    def served_seam_path(self, requested: str, served: str) -> Optional[str]:
        """:meth:`served_path` for a file read through the content seam:
        *served* is the seam-confined path the feature chose for *requested*
        (itself, or its ``.md`` file). None unless it is the canonical,
        allowed spelling."""
        ...

    def project_article(self, raw: str, relative_path: str) -> Optional[str]:
        """The text of the article at *relative_path* as this caller may see
        it (projected when the policy says so), or None when denied."""
        ...

    def filter_articles(
        self, articles: Sequence[Mapping[str, Any]]
    ) -> Sequence[Mapping[str, Any]]:
        """The entries (``path`` without extension) this caller may see."""
        ...

    def filter_sidebar(
        self, sidebar: Sequence[Mapping[str, Any]], uncategorized_label: str
    ) -> Sequence[Mapping[str, Any]]:
        """A filtered copy of a sidebar tree (groups of ``categories`` and
        ``articles``): visible entries, titles and category names projected;
        empty groups dropped. *sidebar* is never modified."""
        ...


@runtime_checkable
class DependencyMapView(Protocol):
    """One caller's readable view of the catalog's dependency map (the
    ``dependency-map/`` documents and ``_domains.json``). Added in 0.7.

    A restricted caller sees only the domains and repositories the catalog
    policy allows, each document projected for display; a domain it may not
    see is answered like a missing one. Labels are catalog-relative
    (``dependency-map/<name>.md``) for a restricted caller. Synchronous file
    I/O: async callers use a worker thread.
    """

    def is_present(self) -> bool:
        """Whether the dependency map exists at all."""
        ...

    def load_domains_json(self) -> List[Dict[str, Any]]:
        """The visible ``_domains.json`` entries (``name``,
        ``participating_repos``, ...)."""
        ...

    def describe(self, entry: Mapping[str, Any]) -> str:
        """The (projected) description of an entry this view returned."""
        ...

    def domain_names(self) -> List[str]:
        """The visible domain documents' names."""
        ...

    def has_domain(self, name: str) -> bool:
        """Whether *name* is a readable document."""
        ...

    def read_text(self, name: str) -> str:
        """The (projected) text of document *name*; ``FileNotFoundError``
        when it is missing or not visible."""
        ...

    def label(self, name: str) -> str:
        """How to name document *name* to this caller."""
        ...

    def resolved_label(self, name: str) -> str:
        """:meth:`label` after symlink resolution (the same for a restricted
        caller)."""
        ...

    def within(self, name: str) -> bool:
        """Whether document *name* is inside the view."""
        ...


@runtime_checkable
class DependencyMapAccess(Protocol):
    """A caller's dependency-map view and what it may see. Added in 0.7."""

    @property
    def restricted(self) -> bool:
        """False for a caller who sees the whole map (an administrator)."""
        ...

    @property
    def visible_repositories(self) -> Optional[FrozenSet[str]]:
        """The repositories a restricted caller may see; None when
        unrestricted."""
        ...

    @property
    def view(self) -> DependencyMapView:
        """The caller's view of the map."""
        ...


@runtime_checkable
class AccessProjectionCapability(Protocol):
    """``access_projection``: the catalog access projection, applied by the
    core above the content seam, once (end state, section 7). A feature that
    serves repository files as documents asks for a caller's view before it
    serves anything."""

    def catalog_view(
        self, viewer: str, repo_dir: str, *, alias: str, owner: Optional[str] = None
    ) -> Optional[CatalogArticleView]:
        """*viewer*'s restricted view of the repository at *repo_dir*, or None
        when nothing is restricted (not the catalog, or an unrestricted
        caller). *alias* is the golden alias, or with *owner* the alias of
        *owner*'s activated repository."""
        ...

    def visible_article_views(
        self, viewer: str, alias: str, rows: Sequence[Mapping[str, Any]]
    ) -> Sequence[Mapping[str, Any]]:
        """Rows keyed by ``article_path`` (catalog-relative, with extension)
        that *viewer* may see of the golden repository *alias*."""
        ...

    def visible_memory_ids(
        self, viewer: str, memory_ids: Sequence[str]
    ) -> Sequence[str]:
        """The ids of the catalog's memory files (``memories/<id>.md``)
        *viewer* may see, in the given order; none when the caller's access
        cannot be decided. Each file's scope is read fresh. Added in 0.7."""
        ...

    def dependency_map_access(self, viewer: str, map_dir: str) -> DependencyMapAccess:
        """*viewer*'s access to the dependency map in *map_dir* (a catalog
        ``dependency-map`` directory): the projected view for a restricted
        caller, the directory as it is for an unrestricted one. Visibility is
        resolved fresh (acting-user scope honoured). Added in 0.7."""
        ...


# ── repository lifecycle events (0.5) ────────────────────────────────────────


@runtime_checkable
class RepositoryEventListener(Protocol):
    """What a feature is told about repositories. Called synchronously on
    the node that made the change, after it was made; a listener must not
    raise (the core logs and drops an exception) and must keep only shared
    state (the change itself is already visible to every node)."""

    def content_changed(self, alias: str, path: Optional[str]) -> None:
        """Files of *alias* changed: *path* (repository-relative) was
        written or deleted, or None for an operation that may have changed
        any file (a git pull, reset, branch switch, refresh)."""
        ...

    def repository_removed(self, alias: str) -> None:
        """The golden repository *alias* was removed."""
        ...

    def repository_renamed(self, old_alias: str, new_alias: str) -> None:
        """The golden repository *old_alias* is now *new_alias*."""
        ...


@runtime_checkable
class RepositoryEventsCapability(Protocol):
    """``repository_events``: subscribe to repository lifecycle events."""

    def subscribe(self, listener: RepositoryEventListener) -> Callable[[], None]:
        """Deliver events to *listener* in this process until the returned
        callable is called (``on_shutdown``)."""
        ...


#: Capability name -> the Protocol its object implements.
CAPABILITY_PROTOCOLS: Mapping[str, type] = {
    CONFIG: ConfigCapability,
    ACCESS_FILTER: AccessFilterCapability,
    PERMISSIONS: PermissionsCapability,
    BACKGROUND_JOBS: BackgroundJobsCapability,
    PAYLOAD_CACHE: PayloadCacheCapability,
    SEARCH_EVENTS: SearchEventsCapability,
    TELEMETRY: TelemetryCapability,
    HTTP_CLIENT: HttpClientCapability,
    STORAGE: StorageCapability,
    HEALTH: HealthCapability,
    AUDIT_LOG: AuditLogCapability,
    CONTENT_SEAM: ContentSeamCapability,
    REGEX_TARGET: RegexTargetCapability,
    SNAPSHOT_PREDICATE: SnapshotPredicateCapability,
    READ_SCOPE: ReadScopeCapability,
    ACCESS_PROJECTION: AccessProjectionCapability,
    REPOSITORY_EVENTS: RepositoryEventsCapability,
}
