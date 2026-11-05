"""The feature contract: a manifest, contribution DTOs and the hooks.

A feature add-on registers ONE entry point in group ``code_indexer.features``
(:data:`cidx_sdk.entry_points.FEATURES_ENTRY_POINT_GROUP`). The entry point
name is the feature name, and its value is a zero-argument factory returning
an object that satisfies :class:`Feature`::

    [project.entry-points."code_indexer.features"]
    example = "cidx_example.feature:make_feature"

No hook argument or return type names a third-party class. Where the core
needs a framework object (a FastAPI router, a Click command) the field is typed
``object`` and its docstring says what the core expects; the core checks it
when it consumes the contribution, not the SDK.

Hooks are synchronous. The core calls ``on_startup``/``on_shutdown`` and
``health`` from a worker thread, never on the event loop, so blocking I/O in
them is allowed (but keep startup short: it delays readiness).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import (
    Callable,
    FrozenSet,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from cidx_sdk.capabilities import KNOWN_CAPABILITIES, FeatureContext
from cidx_sdk.entry_points import SEAM_ENTRY_POINT_GROUPS
from cidx_sdk.errors import InvalidManifestError, InvalidVersionError
from cidx_sdk.version import VersionRange, parse_version

FEATURE_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,62}$"
_FEATURE_NAME_RE = re.compile(FEATURE_NAME_PATTERN)

HEALTHY = "healthy"
DEGRADED = "degraded"
UNHEALTHY = "unhealthy"
HEALTH_STATUSES: FrozenSet[str] = frozenset({HEALTHY, DEGRADED, UNHEALTHY})


def is_valid_feature_name(name: object) -> bool:
    """Whether *name* is a legal feature (and entry point) name."""
    return isinstance(name, str) and _FEATURE_NAME_RE.match(name) is not None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidManifestError(message)


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _str_tuple(value: Sequence[str], what: str) -> Tuple[str, ...]:
    _require(
        not isinstance(value, str), f"{what} must be a sequence of names, not a str"
    )
    items = tuple(value)
    _require(all(isinstance(item, str) for item in items), f"{what} must be strs")
    _require(len(set(items)) == len(items), f"{what} has duplicates: {items!r}")
    return items


@dataclass(frozen=True)
class FeatureManifest:
    """Identity and requirements of a feature, validated on construction.

    Attributes:
        name: the feature name; must equal the entry point name.
        version: the feature's own version (dotted integers).
        requires_sdk: the SDK range the feature was built for, e.g.
            ``">=0.1,<0.2"``. The core refuses the feature outside it.
        required_capabilities: capability names (``cidx_sdk.capabilities``)
            the core must provide, or the feature is refused.
        optional_seams: seam names (keys of ``SEAM_ENTRY_POINT_GROUPS``) the
            feature can use when a backend is installed. Informational today.
        depends_on: other feature names that must be enabled and are started
            before this one.
        description: one line for health output and logs.
    """

    name: str
    version: str
    requires_sdk: str
    required_capabilities: Sequence[str] = ()
    optional_seams: Sequence[str] = ()
    depends_on: Sequence[str] = ()
    description: str = ""

    def __post_init__(self) -> None:
        _require(
            is_valid_feature_name(self.name),
            f"feature name {self.name!r} must match {FEATURE_NAME_PATTERN}",
        )
        try:
            parse_version(self.version)
            VersionRange.parse(self.requires_sdk)
        except InvalidVersionError as exc:
            raise InvalidManifestError(f"feature {self.name!r}: {exc}") from exc
        caps = _str_tuple(self.required_capabilities, "required_capabilities")
        unknown_caps = sorted(set(caps) - KNOWN_CAPABILITIES)
        _require(
            not unknown_caps,
            f"feature {self.name!r} requires unknown capabilities {unknown_caps}",
        )
        seams = _str_tuple(self.optional_seams, "optional_seams")
        unknown_seams = sorted(set(seams) - set(SEAM_ENTRY_POINT_GROUPS))
        _require(
            not unknown_seams,
            f"feature {self.name!r} names unknown seams {unknown_seams}",
        )
        deps = _str_tuple(self.depends_on, "depends_on")
        _require(
            all(is_valid_feature_name(dep) for dep in deps),
            f"feature {self.name!r} has an invalid depends_on entry: {deps!r}",
        )
        _require(self.name not in deps, f"feature {self.name!r} depends on itself")
        _require(isinstance(self.description, str), "description must be a str")
        object.__setattr__(self, "required_capabilities", caps)
        object.__setattr__(self, "optional_seams", seams)
        object.__setattr__(self, "depends_on", deps)


@dataclass(frozen=True)
class ToolContribution:
    """One MCP tool.

    Attributes:
        name: the MCP tool name; unique across all loaded features and the core.
        handler: the core calls it as ``handler(params: dict, user) -> dict``,
            the same shape as a core handler today: it returns the MCP result
            (``{"content": [{"type": "text", "text": ...}]}``), and ``user``
            has ``username`` and ``role``. Called on a worker thread.
        doc_markdown: the tool doc, shipped inside the feature's wheel: YAML
            front matter with ``name`` (equal to ``name``), ``category``,
            ``required_permission``, ``tl_dr`` and ``inputSchema``, then the
            markdown description. The same format as the core's tool docs.
        required_permission: permission name gating the tool, or None to use
            the doc's ``required_permission``. When both are set they must
            agree.
        manages_own_timeout: True when the handler bounds its own runtime (a
            validated ``timeout_seconds`` / ``await_seconds`` parameter, or a
            job id returned for polling): the core's generic per-call
            dispatch timeout is then not applied, so it cannot cut a
            legitimate, handler-approved wait short. Added in SDK 0.5.
    """

    name: str
    handler: Callable[..., object]
    doc_markdown: str
    required_permission: Optional[str] = None
    manages_own_timeout: bool = False

    def __post_init__(self) -> None:
        _require(_non_empty_str(self.name), "tool name must be a non-empty str")
        _require(callable(self.handler), f"tool {self.name!r} handler not callable")
        _require(isinstance(self.doc_markdown, str), "doc_markdown must be a str")
        _require(
            isinstance(self.manages_own_timeout, bool),
            f"tool {self.name!r} manages_own_timeout must be a bool",
        )


#: ``RouterContribution.auth``: the core's API authentication (bearer token,
#: API key or the Web UI's session JWT); an unauthenticated call is a 401.
ROUTER_AUTH_API = "api"
#: ``RouterContribution.auth``: a Web UI session first, else a bearer token or
#: API key -- for pages a browser opens and scripts fetch alike (0.5).
ROUTER_AUTH_HYBRID = "hybrid"
ROUTER_AUTH_MODES = frozenset({ROUTER_AUTH_API, ROUTER_AUTH_HYBRID})


@dataclass(frozen=True)
class RouterContribution:
    """A REST router.

    Every route of the router requires an authenticated caller (bearer
    token, API key or Web UI session); the core checks that before the
    feature's handler runs and puts the caller on the request state
    (:mod:`cidx_sdk.web`).

    Attributes:
        router: the core expects a ``fastapi.APIRouter``.
        prefix: path prefix the core mounts it under ("" for none).
        required_permission: permission every route requires, or None for
            any authenticated caller. Added in SDK 0.2.
        auth: how the caller is authenticated, ``ROUTER_AUTH_API`` (default)
            or ``ROUTER_AUTH_HYBRID``. Added in SDK 0.5.
        login_redirect: an unauthenticated request is redirected to the
            login page (``303``, ``redirect_to`` the requested URL) instead
            of answered 401 -- for routes a browser navigates to. Added in
            SDK 0.5.
        static_dir: a directory served WITHOUT authentication at
            ``<prefix>/_static/`` (the stylesheets and scripts the router's
            pages load), ahead of the router's own routes. Requires a
            non-empty ``prefix``. Added in SDK 0.5.
        nav_label: navigation entry text linking to ``prefix`` (the router's
            page), or None for no entry. Requires a non-empty ``prefix``.
            Added in SDK 0.7.
        nav_permission: permission a session needs to see the nav entry, or
            None for every session. Only the entry: the routes stay gated
            by ``required_permission``. Requires ``nav_label``. Added in
            SDK 0.7.
    """

    router: object
    prefix: str = ""
    required_permission: Optional[str] = None
    auth: str = ROUTER_AUTH_API
    login_redirect: bool = False
    static_dir: Optional[str] = None
    nav_label: Optional[str] = None
    nav_permission: Optional[str] = None

    def __post_init__(self) -> None:
        _require(self.router is not None, "router must not be None")
        _require(isinstance(self.prefix, str), "prefix must be a str")
        _require(
            self.prefix == "" or self.prefix.startswith("/"),
            f"router prefix must be empty or start with '/', got {self.prefix!r}",
        )
        _require(
            self.auth in ROUTER_AUTH_MODES,
            f"router auth must be one of {sorted(ROUTER_AUTH_MODES)}, got {self.auth!r}",
        )
        _require(isinstance(self.login_redirect, bool), "login_redirect must be a bool")
        _require(
            self.static_dir is None or _non_empty_str(self.static_dir),
            "static_dir must be None or a non-empty str",
        )
        _require(
            self.static_dir is None or self.prefix != "",
            "a router with a static_dir needs a prefix (served at <prefix>/_static)",
        )
        _require(
            self.nav_label is None or _non_empty_str(self.nav_label),
            "nav_label must be None or a non-empty str",
        )
        _require(
            self.nav_label is None or self.prefix != "",
            "a router with a nav_label needs a prefix (the entry links to it)",
        )
        _require(
            self.nav_permission is None or self.nav_label is not None,
            "nav_permission needs a nav_label",
        )


@dataclass(frozen=True)
class UiPageContribution:
    """A web UI page rendered inside the core's shell.

    The core requires a Web UI session for every route of ``router`` (a
    visitor without one is sent to the login page), checks
    ``required_permission``, and gives the handler a renderer for its
    templates inside the shell (:mod:`cidx_sdk.web`). The navigation entry
    is shown only to sessions holding ``required_permission``.

    Attributes:
        path: the page route, e.g. ``"/admin/example"``; the nav entry's link.
        title: page title.
        router: the core expects a ``fastapi.APIRouter`` serving ``path``.
        nav_label: navigation entry text, or None for no entry.
        required_permission: permission gating the page, or None.
        template_dirs: Jinja template directories shipped in the wheel.
        static_dir: static file directory shipped in the wheel, or None. The
            core serves it at ``/feature-static/<feature name>/``.
    """

    path: str
    title: str
    router: object
    nav_label: Optional[str] = None
    required_permission: Optional[str] = None
    template_dirs: Sequence[str] = ()
    static_dir: Optional[str] = None

    def __post_init__(self) -> None:
        _require(
            isinstance(self.path, str) and self.path.startswith("/"),
            f"ui page path must start with '/', got {self.path!r}",
        )
        _require(_non_empty_str(self.title), "ui page title must be a non-empty str")
        _require(self.router is not None, "ui page router must not be None")
        object.__setattr__(self, "template_dirs", tuple(self.template_dirs))


@dataclass(frozen=True)
class CliCommandContribution:
    """A subcommand under the ``cidx`` CLI.

    Attributes:
        name: the subcommand name.
        command: the core expects a ``click.Command`` (or ``click.Group``).
    """

    name: str
    command: object

    def __post_init__(self) -> None:
        _require(_non_empty_str(self.name), "cli command name must be non-empty")
        _require(self.command is not None, "cli command must not be None")


@dataclass(frozen=True)
class ConfigKey:
    """One setting of a feature's config section.

    ``bootstrap`` keys live in ``config.json`` and are read before the
    database exists; every other key is a runtime setting stored by the
    config service and edited in the Web UI. Never an environment variable.

    ``minimum`` / ``maximum`` (SDK 0.5) bound a numeric key: the core refuses
    a runtime update outside them, as it refuses a value of the wrong type.

    ``label`` and ``restart_required`` (SDK 0.7) are for the config screen:
    the label shown instead of the key name, and a "requires server restart"
    note for a key read once at startup. Neither changes how a value is
    stored or checked.
    """

    name: str
    default: object
    bootstrap: bool = False
    description: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    label: str = ""
    restart_required: bool = False

    def __post_init__(self) -> None:
        _require(_non_empty_str(self.name), "config key name must be non-empty")
        _require(
            isinstance(self.restart_required, bool),
            f"config key {self.name!r} restart_required must be a bool",
        )
        for bound in (self.minimum, self.maximum):
            _require(
                bound is None
                or (isinstance(bound, (int, float)) and not isinstance(bound, bool)),
                f"config key {self.name!r} bounds must be numbers",
            )
        _require(
            self.minimum is None
            or self.maximum is None
            or self.minimum <= self.maximum,
            f"config key {self.name!r} minimum exceeds maximum",
        )


@dataclass(frozen=True)
class ConfigSection:
    """A feature's config schema: a named section of typed keys.

    ``title`` and ``description`` (SDK 0.7) head the section on the config
    screen (the title defaults to the name).
    """

    name: str
    keys: Sequence[ConfigKey]
    title: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        _require(_non_empty_str(self.name), "config section name must be non-empty")
        keys = tuple(self.keys)
        _require(
            all(isinstance(key, ConfigKey) for key in keys),
            f"config section {self.name!r} keys must be ConfigKey",
        )
        names = [key.name for key in keys]
        _require(
            len(set(names)) == len(names),
            f"config section {self.name!r} has duplicate keys",
        )
        object.__setattr__(self, "keys", keys)

    @property
    def bootstrap_keys(self) -> FrozenSet[str]:
        return frozenset(key.name for key in self.keys if key.bootstrap)

    @property
    def runtime_keys(self) -> FrozenSet[str]:
        return frozenset(key.name for key in self.keys if not key.bootstrap)


@dataclass(frozen=True)
class Migration:
    """One schema migration, tracked per feature.

    Must be backward compatible (rolling restarts share the schema): create
    tables/indexes if not exists, add nullable or defaulted columns; never
    drop, rename or retype.

    Attributes:
        version: strictly increasing within the feature, starting at 1.
        description: one line.
        apply: the core calls it with a DB-API connection for the active
            storage backend (SQLite solo, PostgreSQL cluster).
    """

    version: int
    description: str
    apply: Callable[[object], None]

    def __post_init__(self) -> None:
        _require(
            isinstance(self.version, int)
            and not isinstance(self.version, bool)
            and self.version >= 1,
            f"migration version must be an int >= 1, got {self.version!r}",
        )
        _require(callable(self.apply), "migration apply must be callable")


@dataclass(frozen=True)
class BackgroundJob:
    """A job the core registers with its job manager (dashboard-visible).

    Attributes:
        name: unique within the feature.
        operation_type: the job manager's operation type string.
        run: called with the feature's context on each run.
        interval_seconds: period for a recurring job; None for on-demand.
    """

    name: str
    operation_type: str
    run: Callable[[FeatureContext], None]
    interval_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        _require(_non_empty_str(self.name), "job name must be non-empty")
        _require(_non_empty_str(self.operation_type), "operation_type must be set")
        _require(callable(self.run), f"job {self.name!r} run is not callable")
        _require(
            self.interval_seconds is None or self.interval_seconds > 0,
            "interval_seconds must be positive or None",
        )


@dataclass(frozen=True)
class JobTypeContribution:
    """How the core's job manager treats one operation type the feature's
    handlers submit jobs under (``background_jobs`` capability or the job
    manager directly). Added in SDK 0.5.

    Attributes:
        operation_type: the job manager's operation type string.
        hidden_from_dashboard: True for high-volume, short-lived jobs (one
            per search) that the dashboard's recent-jobs panel leaves out;
            ``/api/jobs`` still lists them.
        repo_alias_optional: True when the feature submits these jobs without
            a ``repo_alias`` by design (no per-repository dedup): the core
            then does not warn about the missing alias.
    """

    operation_type: str
    hidden_from_dashboard: bool = False
    repo_alias_optional: bool = False

    def __post_init__(self) -> None:
        _require(_non_empty_str(self.operation_type), "operation_type must be set")
        _require(
            isinstance(self.hidden_from_dashboard, bool)
            and isinstance(self.repo_alias_optional, bool),
            f"job type {self.operation_type!r} flags must be bools",
        )


#: :attr:`IndexBuildOutcome.status`: the core runs the outcome's ``command``
#: itself, in the repository (cancellable with the owning job, no timeout),
#: and reports its result as it does any index build.
INDEX_BUILD_RUN_LOCALLY = "run_locally"
#: The build was queued to run elsewhere (asynchronously); the index job
#: continues without waiting for it.
INDEX_BUILD_ENQUEUED = "enqueued"
#: An identical build is already queued or running.
INDEX_BUILD_DEDUPED = "deduped"
#: The index already matches the repository's current commit.
INDEX_BUILD_FRESH = "fresh"
#: Nothing was built and nothing will be (fail-soft: the reason is logged);
#: a refresh carries on, an explicit request for the index fails.
INDEX_BUILD_SKIPPED = "skipped"
INDEX_BUILD_STATUSES: FrozenSet[str] = frozenset(
    {
        INDEX_BUILD_RUN_LOCALLY,
        INDEX_BUILD_ENQUEUED,
        INDEX_BUILD_DEDUPED,
        INDEX_BUILD_FRESH,
        INDEX_BUILD_SKIPPED,
    }
)


@dataclass(frozen=True)
class IndexBuildRequest:
    """One repository whose index of a contributed type the core's indexing
    pipeline wants built (a golden repository's refresh or add-index, or an
    activated repository's reindex). Added in SDK 0.5.

    Attributes:
        alias: the repository's alias as the pipeline names it: a golden
            repository's registry alias (may end in ``-global``), or an
            activated repository's user alias.
        repo_root: the repository's root directory on this pod.
        clear: an explicit rebuild: do not answer ``fresh``.
        activated: True for an activated (per-user) repository, which has no
            golden workspace of its own.
    """

    alias: str
    repo_root: str
    clear: bool = False
    activated: bool = False


@dataclass(frozen=True)
class IndexBuildOutcome:
    """What :attr:`IndexBuilderContribution.build` decided.

    Attributes:
        status: one of :data:`INDEX_BUILD_STATUSES`.
        command: with :data:`INDEX_BUILD_RUN_LOCALLY`, the CLI argv the core
            runs in the repository root (required then, empty otherwise).
        detail: a short human note.
    """

    status: str
    command: Sequence[str] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        _require(
            self.status in INDEX_BUILD_STATUSES,
            f"index build status must be one of {sorted(INDEX_BUILD_STATUSES)}, "
            f"got {self.status!r}",
        )
        command = _str_tuple(self.command, "command")
        _require(
            bool(command) == (self.status == INDEX_BUILD_RUN_LOCALLY),
            "an index build outcome carries a command exactly when it is "
            f"{INDEX_BUILD_RUN_LOCALLY!r}",
        )
        object.__setattr__(self, "command", command)


@dataclass(frozen=True)
class IndexBuilderContribution:
    """An index type the core's indexing pipeline builds when the feature is
    enabled (the post-index extension point). Added in SDK 0.5.

    The core owns the pipeline -- which repositories carry the index type,
    refresh scheduling, progress, the snapshot that publishes the index --
    and asks the builder to build. With no builder for a type (the feature
    is not installed or not enabled) the core builds nothing for it, logs
    that, and an explicit request for that type is refused.

    Attributes:
        index_type: the pipeline's name for the index (``"scip"``).
        build: called on a worker thread for one repository; never raises
            (a failure is a ``skipped`` outcome with the reason logged).
        builds_locally: whether a build of this type runs on this pod (the
            ``run_locally`` path) under the current configuration; None means
            always. The core uses it to tell a caller that an add-index
            request completes asynchronously.
        index_present: whether ``repo_root`` already holds this index (the
            registry flag is reconciled against it); None leaves the flag as
            it is.
        annotate_status: given a repository alias and the core's status entry
            for this index type (``get_index_status``), the entry with what
            only the builder knows added (an asynchronous build's state).
            Never raises; None leaves the entry as the core computed it.
    """

    index_type: str
    build: Callable[[IndexBuildRequest], IndexBuildOutcome]
    builds_locally: Optional[Callable[[], bool]] = None
    index_present: Optional[Callable[[str], bool]] = None
    annotate_status: Optional[
        Callable[[str, Mapping[str, object]], Mapping[str, object]]
    ] = None

    def __post_init__(self) -> None:
        _require(
            is_valid_feature_name(self.index_type),
            f"index type {self.index_type!r} must match {FEATURE_NAME_PATTERN}",
        )
        _require(callable(self.build), f"index type {self.index_type!r} build")
        for hook in (self.builds_locally, self.index_present, self.annotate_status):
            _require(
                hook is None or callable(hook),
                f"index type {self.index_type!r} hooks must be callable or None",
            )


@dataclass(frozen=True)
class WebQueryRequest:
    """One query the Web UI query page runs in a contributed search mode.
    Added in SDK 0.5.

    The core resolved the repository the user picked (and checked the
    session) before handing it over.

    Attributes:
        http_request: the core passes the page's ``starlette`` request (for
            ``request.app`` and its search-event writer).
        username: the session's user.
        repository_alias: the alias the user picked.
        repository_path: that repository's root on this pod.
        query_text: the query as typed.
        limit: the page's result limit.
        min_score: the page's minimum-score field, as typed ("" for none).
        options: the page's mode-specific form fields, by name.
    """

    http_request: object
    username: str
    repository_alias: str
    repository_path: str
    query_text: str
    limit: int
    min_score: str = ""
    options: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class WebQueryOutcome:
    """What a contributed search mode answered: result rows in the query
    page's row shape (``file_path``, ``line_numbers``, ``content``, ``score``,
    ``repository_alias``, extra keys allowed; the core adds ``language`` from
    ``file_path`` when a row has none), or the message the page shows
    instead."""

    rows: Sequence[Mapping[str, object]] = ()
    error: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))


@dataclass(frozen=True)
class WebQueryModeContribution:
    """A search mode of the Web UI query page (``search_mode`` form value).
    Added in SDK 0.5.

    The page offers the mode only while the feature is enabled; without it a
    request in that mode is refused like an unknown mode.

    Attributes:
        mode: the ``search_mode`` value, e.g. ``"scip"``.
        execute: runs one query and records its search event; called on a
            worker thread; never raises (a failure is an outcome with
            ``error`` set).
    """

    mode: str
    execute: Callable[[WebQueryRequest], WebQueryOutcome]

    def __post_init__(self) -> None:
        _require(
            is_valid_feature_name(self.mode),
            f"query mode {self.mode!r} must match {FEATURE_NAME_PATTERN}",
        )
        _require(callable(self.execute), f"query mode {self.mode!r} execute")


@dataclass(frozen=True)
class SearchAugmentRequest:
    """One code search a contributed augmenter may add a result section to.
    Added in SDK 0.7.

    The core ran the search (access checked, results reranked) before
    handing it over; the augmenter adds what it knows next to the results.

    Attributes:
        username: the caller.
        query_text: the query as given.
        search_mode: the search's mode (one of the augmenter's
            ``search_modes``).
        limit: the result limit the caller asked for.
        query_vector: the query's embedding, computed once by the core for the
            search and every augmenter; empty when it could not be computed.
        reranker_status: the core reranker's status for this search
            (``"success"``, ``"disabled"``, ``"failed"``, ...).
    """

    username: str
    query_text: str
    search_mode: str
    limit: int
    query_vector: Sequence[float] = ()
    reranker_status: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "query_vector", tuple(self.query_vector))


@dataclass(frozen=True)
class SearchAugmenterContribution:
    """A section a feature adds to the core's single-repository code search
    response (``query_metadata[key]``). Added in SDK 0.7.

    Attributes:
        key: the ``query_metadata`` key the section is answered under, e.g.
            ``"relevant_memories"``; unique across features.
        search_modes: the search modes the section applies to.
        is_active: whether the augmenter runs now (a runtime setting); called
            per search before the core computes the shared query vector, so
            an inactive augmenter costs no embedding call. Never raises.
        augment: the section for one search, or None for no section; called
            on a worker thread after the search; never raises.
        wants_query_vector: the core computes the query's embedding for the
            augmenter when True.
    """

    key: str
    search_modes: Sequence[str]
    is_active: Callable[[], bool]
    augment: Callable[[SearchAugmentRequest], Optional[Sequence[Mapping[str, object]]]]
    wants_query_vector: bool = True

    def __post_init__(self) -> None:
        _require(
            is_valid_feature_name(self.key),
            f"search augmenter key {self.key!r} must match {FEATURE_NAME_PATTERN}",
        )
        modes = tuple(self.search_modes)
        _require(
            bool(modes) and all(is_valid_feature_name(m) for m in modes),
            f"search augmenter {self.key!r} search_modes must be non-empty names",
        )
        object.__setattr__(self, "search_modes", modes)
        _require(callable(self.is_active), f"search augmenter {self.key!r} is_active")
        _require(callable(self.augment), f"search augmenter {self.key!r} augment")


@dataclass(frozen=True)
class HealthReport:
    """One health contributor's current state.

    Attributes:
        component: what is being reported on, e.g. ``"example-cache"``.
        status: one of ``healthy``, ``degraded``, ``unhealthy``.
        detail: JSON-friendly metrics (str/int/float/bool/None values).
        reasons: human-readable reasons when not healthy.
    """

    component: str
    status: str
    detail: Mapping[str, object] = field(default_factory=dict)
    reasons: Sequence[str] = ()

    def __post_init__(self) -> None:
        _require(_non_empty_str(self.component), "component must be non-empty")
        _require(
            self.status in HEALTH_STATUSES,
            f"health status must be one of {sorted(HEALTH_STATUSES)}, "
            f"got {self.status!r}",
        )
        object.__setattr__(self, "reasons", tuple(self.reasons))


@runtime_checkable
class Feature(Protocol):
    """What a ``code_indexer.features`` entry point's factory returns.

    Subclass :class:`BaseFeature` to get empty defaults for every hook.
    """

    @property
    def manifest(self) -> FeatureManifest: ...

    def tools(self) -> Sequence[ToolContribution]: ...

    def routers(self) -> Sequence[RouterContribution]: ...

    def ui_pages(self) -> Sequence[UiPageContribution]: ...

    def cli_commands(self) -> Sequence[CliCommandContribution]: ...

    def config_section(self) -> Optional[ConfigSection]: ...

    def migrations(self) -> Sequence[Migration]: ...

    def health(self) -> Sequence[HealthReport]: ...

    def on_startup(self, ctx: FeatureContext) -> None: ...

    def on_shutdown(self, ctx: FeatureContext) -> None: ...

    def background_jobs(self) -> Sequence[BackgroundJob]: ...

    def job_types(self) -> Sequence[JobTypeContribution]: ...

    def index_builders(self) -> Sequence[IndexBuilderContribution]: ...

    def web_query_modes(self) -> Sequence[WebQueryModeContribution]: ...

    def search_augmenters(self) -> Sequence[SearchAugmenterContribution]: ...


class BaseFeature:
    """Convenience base: every hook contributes nothing until overridden."""

    def __init__(self, manifest: FeatureManifest) -> None:
        if not isinstance(manifest, FeatureManifest):
            raise InvalidManifestError("manifest must be a FeatureManifest")
        self._manifest = manifest

    @property
    def manifest(self) -> FeatureManifest:
        return self._manifest

    def tools(self) -> Sequence[ToolContribution]:
        return ()

    def routers(self) -> Sequence[RouterContribution]:
        return ()

    def ui_pages(self) -> Sequence[UiPageContribution]:
        return ()

    def cli_commands(self) -> Sequence[CliCommandContribution]:
        return ()

    def config_section(self) -> Optional[ConfigSection]:
        return None

    def migrations(self) -> Sequence[Migration]:
        return ()

    def health(self) -> Sequence[HealthReport]:
        return ()

    def on_startup(self, ctx: FeatureContext) -> None:
        return None

    def on_shutdown(self, ctx: FeatureContext) -> None:
        return None

    def background_jobs(self) -> Sequence[BackgroundJob]:
        return ()

    def job_types(self) -> Sequence[JobTypeContribution]:
        return ()

    def index_builders(self) -> Sequence[IndexBuilderContribution]:
        return ()

    def web_query_modes(self) -> Sequence[WebQueryModeContribution]:
        return ()

    def search_augmenters(self) -> Sequence[SearchAugmenterContribution]:
        return ()
