"""Capability names and the :class:`FeatureContext` a feature receives.

A capability is a platform service the core offers a feature (configuration,
background jobs, payload cache, ...). A feature declares the names it needs in
its manifest; the core refuses to load it when one is not provided, and hands
the feature a :class:`FeatureContext` at startup through which it asks for each
one by name.

The names are fixed here; the TYPE of each capability object is the Protocol
of the same name in :mod:`cidx_sdk.capability_types`
(``CAPABILITY_PROTOCOLS[name]``). ``capability()`` returns ``object`` so this
module stays free of those types; narrow it to that Protocol with ``typing.cast``.
"""

from __future__ import annotations

from typing import FrozenSet, Protocol, runtime_checkable

CONFIG = "config"
ACCESS_FILTER = "access_filter"
PERMISSIONS = "permissions"
BACKGROUND_JOBS = "background_jobs"
PAYLOAD_CACHE = "payload_cache"
SEARCH_EVENTS = "search_events"
TELEMETRY = "telemetry"
HTTP_CLIENT = "http_client"
STORAGE = "storage"
HEALTH = "health"
AUDIT_LOG = "audit_log"
# Added in SDK 0.2: the kernel interfaces the X-Ray and SCIP packages already
# reach through (code_indexer.kernel.capabilities in the core).
CONTENT_SEAM = "content_seam"
REGEX_TARGET = "regex_target"
SNAPSHOT_PREDICATE = "snapshot_predicate"
READ_SCOPE = "read_scope"
# Added in SDK 0.5: the catalog access projection a feature applies to what it
# serves (the wiki), and repository lifecycle events (content changed,
# removed, renamed) a feature keeps derived state in step with.
ACCESS_PROJECTION = "access_projection"
REPOSITORY_EVENTS = "repository_events"

KNOWN_CAPABILITIES: FrozenSet[str] = frozenset(
    {
        CONFIG,
        ACCESS_FILTER,
        PERMISSIONS,
        BACKGROUND_JOBS,
        PAYLOAD_CACHE,
        SEARCH_EVENTS,
        TELEMETRY,
        HTTP_CLIENT,
        STORAGE,
        HEALTH,
        AUDIT_LOG,
        CONTENT_SEAM,
        REGEX_TARGET,
        SNAPSHOT_PREDICATE,
        READ_SCOPE,
        ACCESS_PROJECTION,
        REPOSITORY_EVENTS,
    }
)


@runtime_checkable
class FeatureContext(Protocol):
    """What the core hands a feature in ``on_startup`` / ``on_shutdown``.

    One context per feature: ``capability()`` only returns capabilities the
    core provides, and the core never passes a feature a context belonging to
    another feature.
    """

    @property
    def feature_name(self) -> str:
        """The name of the feature this context was built for."""
        ...

    @property
    def sdk_version(self) -> str:
        """The SDK version the running core was built against."""
        ...

    @property
    def core_version(self) -> str:
        """The running core's version string."""
        ...

    def provided_capabilities(self) -> FrozenSet[str]:
        """Every capability name the running core provides."""
        ...

    def has_capability(self, name: str) -> bool:
        """Whether ``capability(name)`` would succeed."""
        ...

    def capability(self, name: str) -> object:
        """The capability object registered under *name*.

        Raises:
            CapabilityUnavailableError: the core does not provide *name*.
        """
        ...
