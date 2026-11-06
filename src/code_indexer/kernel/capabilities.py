"""What the platform offers X-Ray, SCIP and, later, every feature.

Each capability is a Protocol and a :class:`CapabilitySlot` the host installs
an implementation into. The server installs its implementations when its
package is imported (``server/kernel_wiring.py``); a process that never loads
the server -- a library test, a client -- finds every slot empty, and each
consumer helper below then answers what the server itself answers outside a
running server: content not delegated, local ripgrep, in-pod X-Ray execution,
no telemetry, no read scope.

A slot is process wiring, set once and identical on every node; it is never a
store for request state (that stays in the payload cache / job tracker).

No imports of ``code_indexer.server``. The capability Protocols and the slot
type now live in ``cidx-sdk`` (``cidx_sdk.capability_types``,
``cidx_sdk.slots``), the interfaces handed to a feature through
``FeatureContext``; this module re-exports them under their kernel names,
binds the seam-shaped ones to the kernel's seam types, and keeps the slots and
no-host helpers X-Ray and SCIP call. ``XrayPlacementHost`` and
``XrayCacheStoreHost`` stay here: they are X-Ray's own, and move into the
X-Ray feature with it.
"""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Type,
)

from cidx_sdk.capability_types import (
    ApplicationMetrics,
    AttributeValue,
    ContentSeamCapability,
    Meter,
    MetricCounter,
    ReadScopeCapability,
    RegexTargetCapability,
    RuntimeConfigProvider,
    TelemetryCapability,
)
from cidx_sdk.slots import CapabilitySlot

from code_indexer.kernel.seam_types import (
    ContentEntry,
    ContentSeamSource,
    RipgrepBackend,
    XrayExecutionBackend,
)

__all__ = [
    "ApplicationMetrics",
    "AttributeValue",
    "CapabilitySlot",
    "ContentSeamHost",
    "Meter",
    "MetricCounter",
    "ReadScopeHost",
    "RegexTargetHost",
    "RuntimeConfigHost",
    "ServerPlatformHost",
    "TelemetryHost",
    "XrayCacheStoreHost",
    "XrayPlacementHost",
]

# ── content seam ─────────────────────────────────────────────────────────────


#: Reads a repository's files when they are not on this machine.
ContentSeamHost = ContentSeamCapability[ContentSeamSource, ContentEntry]


CONTENT_SEAM_HOST: CapabilitySlot[ContentSeamHost] = CapabilitySlot("content seam")


def content_seam_delegated() -> bool:
    """Whether repository files are read through a non-local content backend."""
    host = CONTENT_SEAM_HOST.get()
    return host is not None and host.is_delegated()


def content_seam_source(repo_path: str, *, consumer: str) -> ContentSeamSource:
    """The content seam for *repo_path* -- always one.

    The host's answer; without a host (a process that never loads the
    server) the content seam's ``local`` implementation, reading *repo_path*
    itself. Whether the files are a directory here is :func:`pod_directory`.
    """
    host = CONTENT_SEAM_HOST.get()
    source = None if host is None else host.source_for(repo_path, consumer=consumer)
    if source is None:
        return ContentSeamSource(backend=_local_content_backend(), root=repo_path)
    return source


_local_backend_lock = threading.Lock()
_local_backend: List[Any] = []


def _local_content_backend() -> Any:
    """The content seam's ``local`` implementation, resolved once per process
    (raises, naming the seam, when nothing is installed for the mode)."""
    with _local_backend_lock:
        if not _local_backend:
            from code_indexer.kernel.content_seam import content_backend_for

            _local_backend.append(content_backend_for())
        return _local_backend[0]


def pod_directory(source: ContentSeamSource) -> Optional[Path]:
    """The directory on this machine holding *source*'s files, or None.

    The implementation's own answer (``is_local``): a reader whose local
    behaviour is richer than the Protocol (links followed inside the
    repository) keeps it for such a source; any other reads the source.
    """
    return Path(source.root) if source.backend.is_local else None


def iter_manifest_entries(
    source: ContentSeamSource,
    prune_dirs: Sequence[str],
    *,
    deadline: Optional[float] = None,
) -> Iterator[ContentEntry]:
    """The manifest of *source*, through the host that produced it.

    Raises:
        RuntimeError: no content seam host is installed (a source exists
            only where one is).
    """
    host = CONTENT_SEAM_HOST.get()
    if host is None:
        raise RuntimeError("no content seam host is installed")
    return host.iter_manifest_entries(source, prune_dirs, deadline=deadline)


# ── ripgrep placement ────────────────────────────────────────────────────────


#: Where a regex search over a repository runs.
RegexTargetHost = RegexTargetCapability[RipgrepBackend]


REGEX_TARGET_HOST: CapabilitySlot[RegexTargetHost] = CapabilitySlot("regex target")


def regex_target(repo_path: Path) -> Tuple[Path, Optional[RipgrepBackend]]:
    """The host's answer, or *repo_path* searched locally when there is none."""
    host = REGEX_TARGET_HOST.get()
    return (Path(repo_path), None) if host is None else host.regex_target(repo_path)


# ── X-Ray execution placement and evaluator cache store ──────────────────────


class XrayPlacementHost(Protocol):
    """Where an X-Ray evaluator is executed."""

    def configured_backend(self) -> Optional[XrayExecutionBackend]:
        """The execution backend the host selected, or None for in-process."""
        ...

    def backend_for(
        self,
        configured: Optional[XrayExecutionBackend],
        repo_path: Optional[str],
        rel_paths: Sequence[str],
    ) -> Optional[XrayExecutionBackend]:
        """Where ONE request over *repo_path* executes; None means in-process.
        May raise to refuse the request. Blocking: call off the event loop."""
        ...


XRAY_PLACEMENT_HOST: CapabilitySlot[XrayPlacementHost] = CapabilitySlot(
    "X-Ray placement"
)


def configured_xray_execution_backend() -> Optional[XrayExecutionBackend]:
    """The host's configured X-Ray backend; None (in-process) without a host."""
    host = XRAY_PLACEMENT_HOST.get()
    return None if host is None else host.configured_backend()


def xray_execution_backend_for(
    configured: Optional[XrayExecutionBackend],
    repo_path: Optional[str],
    rel_paths: Sequence[str],
) -> Optional[XrayExecutionBackend]:
    """The host's per-request decision; *configured* unchanged without a host."""
    host = XRAY_PLACEMENT_HOST.get()
    if host is None:
        return configured
    return host.backend_for(configured, repo_path, rel_paths)


class XrayCacheStoreHost(Protocol):
    """Opens the database the cluster-wide X-Ray evaluator cache lives in.

    The host hands out a connection pool (an object whose ``connection()``
    is a context manager yielding a DB-API connection); X-Ray builds its own
    store over it, so the core never imports the feature.
    """

    def postgres_connection_pool(self, postgres_dsn: str) -> Any:
        """A connection pool over the PostgreSQL database at *postgres_dsn*."""
        ...


XRAY_CACHE_STORE_HOST: CapabilitySlot[XrayCacheStoreHost] = CapabilitySlot(
    "X-Ray cache store"
)


# ── runtime configuration ────────────────────────────────────────────────────


class DescriptionFactCheckHost(Protocol):
    """The post-generation fact check of a repository description (Story
    #724 v2). The dependency-map feature (cidx-depmap) installs it at
    ``on_startup``: the check is its analyzer's verification pass, enabled by
    its ``dep_map_fact_check_enabled`` setting. Without the feature the slot
    is empty and descriptions are written unverified."""

    def enabled(self) -> bool:
        """Whether descriptions are fact-checked now (a runtime setting)."""
        ...

    def verify(
        self,
        document_path: Path,
        repo_list: List[Dict[str, Any]],
        golden_repos_dir: Path,
        cidx_meta_path: Path,
    ) -> bool:
        """Verify the document at *document_path* against *repo_list* in
        place; True when it was verified, False to keep the unverified text.
        Blocking (an LLM CLI run)."""
        ...


DESCRIPTION_FACT_CHECK_HOST: CapabilitySlot[DescriptionFactCheckHost] = CapabilitySlot(
    "description fact check"
)


#: The server's runtime configuration (the config service, never env vars).
RuntimeConfigHost = RuntimeConfigProvider


RUNTIME_CONFIG_HOST: CapabilitySlot[RuntimeConfigHost] = CapabilitySlot(
    "runtime config"
)


def runtime_config() -> Optional[object]:
    """The host's runtime configuration, or None when no host is installed."""
    host = RUNTIME_CONFIG_HOST.get()
    return None if host is None else host.runtime_config()


# ── telemetry ────────────────────────────────────────────────────────────────

#: Metrics and spans. Never creates telemetry that is not already running.
TelemetryHost = TelemetryCapability


TELEMETRY_HOST: CapabilitySlot[TelemetryHost] = CapabilitySlot("telemetry")


def application_metrics() -> Optional[ApplicationMetrics]:
    """Active application metrics, or None (also when no host is installed)."""
    host = TELEMETRY_HOST.get()
    return None if host is None else host.application_metrics()


def application_meter() -> Optional[Meter]:
    """The active ``cidx.application`` meter, or None."""
    host = TELEMETRY_HOST.get()
    return None if host is None else host.application_meter()


def telemetry_span(
    name: str, attributes: Optional[Dict[str, Any]] = None
) -> AbstractContextManager[Any]:
    """A span from the host, or a no-op context when there is none."""
    host = TELEMETRY_HOST.get()
    return nullcontext() if host is None else host.span(name, attributes)


# ── served-index read scope ──────────────────────────────────────────────────


#: Keeps a read's served index checkouts held while another thread uses them.
ReadScopeHost = ReadScopeCapability


READ_SCOPE_HOST: CapabilitySlot[ReadScopeHost] = CapabilitySlot("read scope")


def carry_read_scope(fn: Callable[[], None]) -> Callable[[], None]:
    """*fn* carrying the running read scope; *fn* unchanged without a host."""
    host = READ_SCOPE_HOST.get()
    return fn if host is None else host.carry(fn)


# ── server platform (transitional) ───────────────────────────────────────────


class ServerPlatformHost(Protocol):
    """Server pieces a feature add-on still reaches outside ``cidx_sdk``.

    The SDK capabilities (``FeatureContext``) cover configuration, jobs,
    payload cache, telemetry, storage, permissions, access filtering and
    search events in their portable form. The X-Ray add-on's MCP handlers and
    REST routes need richer forms of several of them -- the job tracker's
    lifecycle, paged payload-cache writes, the X-Ray search-event recorder,
    the group-based repository guard -- plus repository resolution, query
    admission and the content seam's manifest reads, which have no SDK
    capability yet. This slot is the one place they come from, so a feature
    imports no server module (``cidx_xray._platform`` reads it). Each group of
    methods is a candidate SDK capability; when the SDK grows one, its methods
    leave this Protocol.

    Every method is blocking and resolves its server object when CALLED, so a
    test that patches a server binding is observed. The server installs the
    implementation (``code_indexer.server.feature_platform``) when its package
    is imported; outside a server process the slot is empty.
    """

    # app services ------------------------------------------------------------
    def app_attr(self, name: str) -> Any:
        """The server app module's already-built ``name`` (``app``,
        ``job_tracker``, ``background_job_manager``, ``golden_repo_manager``,
        ``activated_repo_manager``), or None -- never builds it."""
        ...

    # repositories --------------------------------------------------------------
    def resolve_repo_path(self, alias: str) -> Optional[str]:
        """A global alias's read path, registry ``index_path`` fallback included."""
        ...

    def resolve_golden_repo_path(self, alias: str) -> Optional[str]:
        """A global alias's versioned snapshot path (pointer only)."""
        ...

    def try_global_fallback(
        self, alias: str, golden_repo_manager: Any
    ) -> Optional[str]:
        """``<alias>-global`` when *alias* is a bare golden alias, else None."""
        ...

    def refresh_scheduler(self) -> Any:
        """The running refresh scheduler (cidx-meta write lock), or None."""
        ...

    # MCP response shaping ----------------------------------------------------
    def mcp_response(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """*data* in the MCP ``content`` envelope, serialized as core tools are."""
        ...

    def parse_json_string_array(self, value: Any) -> Any:
        """A JSON-encoded string array decoded; anything else unchanged."""
        ...

    def parse_and_collapse_repo_alias(self, value: Any) -> Any:
        """As above, and a one-element list collapsed to its string."""
        ...

    def enforce_repo_count_cap(self, aliases: Any) -> Any:
        """The omni fan-out cap breach for *aliases*, or None."""
        ...

    def cap_breach_response(self, breach: Any) -> Dict[str, Any]:
        """The MCP error response for a cap breach."""
        ...

    # query admission ---------------------------------------------------------
    def check_query_admission(self) -> Any:
        """The memory-pressure admission decision (``.allowed``)."""
        ...

    def memory_pressure_payload(self, admission: Any) -> Dict[str, Any]:
        """The MCP payload refusing a query under memory pressure."""
        ...

    # search events -----------------------------------------------------------
    def xray_search_recorder(
        self,
        *,
        app: Any,
        tool: str,
        door: str,
        username: str,
        repo_alias: Optional[str],
        pattern: str,
        batch_repositories: Optional[Sequence[str]] = None,
    ) -> Callable[..., None]:
        """An X-Ray search-event recorder bound now (latency clock starts),
        writing to *app*'s event writer (None: the server app)."""
        ...

    def run_recorded_xray(
        self,
        record_event: Callable[..., None],
        run: Callable[[], Dict[str, Any]],
        cancelled: Optional[Callable[[], bool]],
    ) -> Dict[str, Any]:
        """Run one X-Ray search and record it (a failure row when it raises)."""
        ...

    def tracked_job_cancelled(self, tracker: Any, job_id: str) -> bool:
        """Whether a job-tracker job was cancelled while it ran."""
        ...

    # paged payload cache -----------------------------------------------------
    def truncate_result_fields(
        self,
        result: Dict[str, Any],
        payload_cache: Any,
        fields: Sequence[str],
        *,
        owner: Optional[str],
    ) -> Dict[str, Any]:
        """*result* with *fields* paged into the payload cache; raises
        :meth:`page_set_store_error` when the page set could not be stored."""
        ...

    def page_set_store_error(self) -> Type[Exception]:
        """The error :meth:`truncate_result_fields` raises on a failed write."""
        ...

    # repository access -------------------------------------------------------
    def require_repo_access(
        self, service: Any, username: str, aliases: Any
    ) -> Optional[Tuple[str, str]]:
        """None when *username* may read every alias; else ``(error_code,
        message)``: ``access_denied`` or ``access_control_unavailable`` (no
        *service*: fails closed)."""
        ...

    def repository_name_granted(self, username: str, alias: str) -> bool:
        """Whether *username* is an admin or is granted *alias* as a golden
        repository name (the dispatcher's repository guard); False without
        an access filtering service (fails closed)."""
        ...

    def is_admin_role(self, user: Any) -> bool:
        """Whether the authenticated *user* holds the admin role."""
        ...

    # catalog access projection -------------------------------------------------
    def golden_catalog_surface_refusal(
        self,
        surface: str,
        alias: Any,
        username: str,
        access_service: Any,
        golden_repos_dir: Optional[str],
    ) -> Optional[str]:
        """The core's refusal text when *alias* (a golden repository) may
        serve the internal catalog and *username* is restricted, for the
        catalog surface named *surface* (a ``CatalogSurface`` member name),
        else None. Synchronous: reads the group store and alias pointers."""
        ...

    # audit -------------------------------------------------------------------
    def conforming_audit_details(
        self, action_type: str, **candidates: Any
    ) -> Dict[str, Any]:
        """*candidates* kept only where they fit *action_type*'s allowlist."""
        ...

    def record_audit_outcome(
        self,
        *,
        actor: str,
        action_type: str,
        target_type: str,
        target_id: Any,
        outcome: str,
        details: Dict[str, Any],
    ) -> None:
        """Record one audit outcome row; never raises."""
        ...

    # content seam ------------------------------------------------------------
    def always_excluded_dirs(self) -> Sequence[str]:
        """Directories every repository file listing prunes."""
        ...

    def supports_scoped_manifest(self, backend: Any) -> bool:
        """Whether *backend* lists one directory without a full manifest."""
        ...

    def scoped_manifest_entries(
        self, seam: Any, rel_dir: str, *, max_depth: int, prune_dirs: Sequence[str]
    ) -> Sequence[Any]:
        """One directory's manifest entries through *seam*."""
        ...

    def manifest_entries(self, seam: Any, prune_dirs: Sequence[str]) -> Sequence[Any]:
        """Every manifest entry of *seam* outside *prune_dirs*."""
        ...

    def manifest_entries_cached(
        self, seam: Any, prune_dirs: Sequence[str]
    ) -> Optional[Sequence[Any]]:
        """The TTL-cached manifest the file tools share, or None."""
        ...

    def fetch_one(self, seam: Any, rel_path: str) -> Any:
        """One file through *seam* (``.data`` or ``.error``)."""
        ...

    def is_pod_local_golden_path(self, path: str) -> bool:
        """Whether *path* is a pod-local (``local://``) golden repository."""
        ...

    def golden_repo_content_root(self, path: str, *, consumer: str) -> Any:
        """Where the content seam reads the repository at *path*."""
        ...

    def delegated_repository_unsupported(self, feature: str, detail: str) -> Exception:
        """The core's refusal for work this pod cannot do under delegation."""
        ...

    # cidx-meta ---------------------------------------------------------------
    def shared_cidx_meta(self, cidx_meta: Any) -> Any:
        """The shared (delegated) cidx-meta store, or None for local disk."""
        ...

    def cidx_meta_conflict_error(self) -> Type[Exception]:
        """Raised by a shared cidx-meta write whose expectation failed."""
        ...

    def cidx_meta_unconditional(self) -> Any:
        """The shared cidx-meta write expectation "overwrite whatever is there"."""
        ...

    def commit_cidx_meta_files(
        self, cidx_meta: Any, relative_files: Sequence[str], message: str
    ) -> None:
        """git add + commit *relative_files* in the local cidx-meta repo as the
        backup identity, then sync the backup when configured. Raises
        ``subprocess.CalledProcessError`` when git fails."""
        ...

    def request_cidx_meta_refresh(self, scheduler: Any) -> None:
        """Ask *scheduler* for a cidx-meta refresh after a write made outside
        its write lock; never raises (a refresh it cannot run now is owed to
        the debouncer)."""
        ...

    # execution resources -----------------------------------------------------
    def resizable_limiter(self, *, initial: int, k_min: int, k_max: int) -> Any:
        """A resizable concurrency limiter (``acquire``/``release``/``set_limit``)."""
        ...

    def xray_executor_pool_size(self) -> int:
        """The X-Ray job thread pool size (background jobs config)."""
        ...

    def attach_governed_cache(self, cache: Any) -> Callable[[], None]:
        """Put *cache* under the memory governor's eviction; returns detach."""
        ...

    # documents served from repositories (the wiki) ---------------------------
    def backend_registry_attr(self, name: str, caller: str) -> Any:
        """The storage backend registry's *name* backend (the shared
        PostgreSQL one in cluster mode), or None before the registry exists."""
        ...

    def sqlite_connection_manager(self, db_path: str) -> Any:
        """The process's connection manager for the SQLite database
        *db_path* (``get_connection``, ``execute_atomic``)."""
        ...

    def read_alias_target(
        self, golden_repos_dir: str, alias_name: str
    ) -> Optional[str]:
        """The target path of global alias *alias_name* (pointer only)."""
        ...

    def resolve_alias_or_index_path(
        self, golden_repos_dir: str, alias_name: str, repo_entry: Any
    ) -> Optional[str]:
        """The alias pointer's target, else the registry entry's ``index_path``."""
        ...

    def activated_repo_query(
        self, query_tracker: Any, activated_repo_manager: Any, username: str, alias: str
    ) -> Any:
        """Context manager holding a query reference on *username*'s activated
        *alias*, so a concurrent deactivation waits for the query."""
        ...

    def enforce_activated_repo_access(
        self, access_service: Any, username: str, alias: str, refusal: Any
    ) -> None:
        """Raise *refusal* unless *username* still holds grants on the golden
        repositories its activated *alias* comes from."""
        ...

    def is_access_admin(self, access_service: Any, username: str) -> bool:
        """Whether *username* is in the admins group."""
        ...

    def run_recorded_search(
        self,
        app: Any,
        run: Callable[[], Any],
        *,
        tool: str,
        door: str,
        username: str,
        repo_alias: str,
        search_type: str,
        query_text: str,
    ) -> Any:
        """Run a synchronous search door and record its search event."""
        ...

    def sanitize_article_html(self, html: str) -> str:
        """Rendered markdown article HTML through the article allowlist."""
        ...

    def wiki_enabled_golden_aliases(self) -> Set[str]:
        """Golden repository aliases (no ``-global``) whose wiki is enabled."""
        ...

    # Web UI admin actions ----------------------------------------------------
    def require_admin_elevation(self, request: Any) -> None:
        """Raise the core's HTTP error unless the caller is an administrator
        with an active elevation window (when enforcement is on)."""
        ...

    def admin_web_session(self, request: Any) -> Any:
        """The Web UI admin session of *request*, or None."""
        ...

    def login_redirect_response(self, request: Any) -> Any:
        """A redirect to the login page, back to the requested URL."""
        ...

    def validate_login_csrf(self, request: Any, token: Optional[str]) -> bool:
        """Whether *token* is the request's Web UI CSRF token."""
        ...

    def web_error_partial(self, request: Any, message: str, status_code: int) -> Any:
        """The Web UI error-message partial."""
        ...

    def golden_repos_page_response(
        self, request: Any, session: Any, success_message: str
    ) -> Any:
        """The golden repositories admin page with *success_message*."""

    # services (SCIP, phase 4d) -----------------------------------------------
    def config_service(self) -> Any:
        """The server's config service (``get_config()``; reranking takes it)."""
        ...

    def running_app_state(self) -> Any:
        """The running server's ``app.state``, or None -- never builds the app."""
        ...

    def golden_repo_manager(self) -> Any:
        """The golden repo manager singleton (raises when none is registered)."""
        ...

    def golden_index_backfill(self, golden_repos_dir: str) -> Any:
        """The golden-repo index backfill engine (``run_cycle(policy, ...)``:
        the shared scan, cooldown marker and per-cycle bound)."""
        ...

    # logging -----------------------------------------------------------------
    def format_error_log(self, error_code: str, message: str, **context: Any) -> str:
        """``[CODE] message key=value...`` (the server's log line format)."""
        ...

    def log_extra(self, error_code: str) -> Dict[str, Any]:
        """The ``extra`` dict of a coded log line (code, correlation id)."""
        ...

    def correlation_id(self) -> Optional[str]:
        """The current request's correlation id, or None."""
        ...

    def parse_json_column(
        self, raw: object, expected_type: Type[Any], field_name: str
    ) -> Any:
        """A DB row's JSON-bearing value as *expected_type*, or None."""
        ...

    # MCP handler gates -------------------------------------------------------
    def admin_role_required_response(self, user: Any) -> Optional[Dict[str, Any]]:
        """The MCP refusal for a caller without the admin role, or None."""
        ...

    def require_mcp_elevation(self, required_scope: str) -> Callable[..., Any]:
        """The core's TOTP elevation decorator for an MCP handler."""
        ...

    def apply_reranking(self, **kwargs: Any) -> Tuple[List[Any], Dict[str, Any]]:
        """Rerank MCP results (the core's reranker chain); no-op without a
        rerank query. Returns ``(results, rerank_metadata)``."""
        ...

    def api_metrics(self) -> Any:
        """The REST API metrics service (``increment_*``)."""
        ...

    # search events (SCIP) ----------------------------------------------------
    def search_origin(self, tool: str, door: str) -> Any:
        """The core's ``SearchOrigin`` for *tool* through *door*."""
        ...

    def search_event_writer(self, app: Any) -> Any:
        """*app*'s search-event writer, or None when it is not wired."""
        ...

    def search_event_context(self) -> Any:
        """The current search event context, or None."""
        ...

    def search_event_scope(self, **fields: Any) -> AbstractContextManager[Any]:
        """Install a fresh search event context for one search."""
        ...

    def note_current_repositories(self, *args: Any, **kwargs: Any) -> None:
        """Note on the current context the repositories a search read."""
        ...

    def note_current_resolution(self, *args: Any, **kwargs: Any) -> None:
        """Note on the current context the repositories a search resolved."""
        ...

    def repository_resolved(self, *args: Any, **kwargs: Any) -> Any:
        """The search-event outcome of one resolved repository."""
        ...

    def scip_payload_result_count(self, payload: Dict[str, Any]) -> int:
        """How many results a SCIP response payload returns."""
        ...

    def scip_callchain_query_text(self, from_symbol: Any, to_symbol: Any) -> str:
        """The recorded query text of a SCIP call chain search."""
        ...

    def record_scip_search(
        self, writer: Any, payload: Dict[str, Any], **kwargs: Any
    ) -> None:
        """Record one SCIP search from its response payload. Never raises."""
        ...

    def record_multi_repository_search(
        self, writer: Any, grouped: Any, **kwargs: Any
    ) -> None:
        """Record a completed multi-repository search. Never raises."""
        ...

    def recording_multi_repository_failure(
        self, writer: Any, **kwargs: Any
    ) -> AbstractContextManager[None]:
        """Wrap a multi-repository door: a failure row per resolved repo."""
        ...

    def search_events_module(self) -> Any:
        """The search-event recording module (the web query page records
        through its door helpers)."""
        ...

    # errors ------------------------------------------------------------------
    def access_unavailable_error(self) -> Type[Exception]:
        """Raised when the repository access service is not available."""
        ...

    def repo_access_denied_error(self) -> Type[Exception]:
        """Raised when a caller may not read a repository."""
        ...

    def require_repo_access_or_raise(
        self, service: Any, username: str, aliases: Any
    ) -> None:
        """The repository guard, raising :meth:`repo_access_denied_error` or
        :meth:`access_unavailable_error`."""
        ...

    def enforce_repo_access_http(
        self, service: Any, username: str, aliases: Any
    ) -> None:
        """The REST repository guard: an HTTP 403 for a repository the caller
        may not read, an HTTP 500 without the access service."""
        ...

    def unresolved_repository_error(self) -> Type[Exception]:
        """A multi-repository search's unresolved-alias error."""
        ...

    def search_request_error(self) -> Type[Exception]:
        """A web search request the query manager refused."""
        ...

    def duplicate_job_errors(self) -> Tuple[Type[Exception], ...]:
        """The job tracker's and the jobs backend's duplicate-job errors."""
        ...

    def job_status(self) -> Any:
        """The background job status enum."""
        ...

    # served indexes ----------------------------------------------------------
    def served_index_reads(self) -> AbstractContextManager[Any]:
        """Hold the served-index checkouts a read uses for the block."""
        ...

    def active_served_index_cache(self) -> Any:
        """The pod's served-index cache, or None (local store, CLI, tests)."""
        ...

    # repository reads --------------------------------------------------------
    def server_content_backend(self) -> Any:
        """The configured content backend (``is_local``), or None."""
        ...

    def server_delegated_git_backend(self) -> Any:
        """The delegated git backend (``rev_parse``), or None under local."""
        ...

    def build_commit_marker(self) -> str:
        """The index freshness marker's file name."""
        ...

    def current_commit(self, repo_path: Path) -> Optional[str]:
        """A clone's HEAD commit read from ``.git``, or None."""
        ...

    # self-healing ------------------------------------------------------------
    def git_state_manager(self, config: Any) -> Any:
        """A git state manager (branch, commit, PR after a dependency fix)."""
        ...

    def extract_json_from_llm_response(self, text: str) -> Any:
        """The JSON object an LLM reply carries."""
        ...


SERVER_PLATFORM_HOST: CapabilitySlot[ServerPlatformHost] = CapabilitySlot(
    "server platform"
)
