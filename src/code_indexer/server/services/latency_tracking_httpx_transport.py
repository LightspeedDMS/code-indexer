"""
HTTPX transport wrappers that measure per-request wall-clock latency.

Story #680: External Dependency Latency Observability

Provides:
- DependencyRegistry: immutable (host, path_prefix) → dependency_name resolver
  using longest-prefix matching. Constructed from a tuple of entries; safe for
  concurrent read access with no synchronization needed.
- DependencyRegistryBuilder: mutable builder used at startup to accumulate
  registry entries, then frozen via build() before the registry is shared.
- LatencyTrackingHTTPXTransport: sync httpx.HTTPTransport wrapper.
- AsyncLatencyTrackingHTTPXTransport: async httpx.AsyncHTTPTransport wrapper.
- DEFAULT_REGISTRY_ENTRIES: the 4 VoyageAI/Cohere URL→dep_name mappings
  required by the story spec (Algorithm 2). These are the contractual production
  API endpoints used during server startup wiring — not arbitrary hardcoding.

Both transport wrappers record status_code=-1 on exception and always
re-raise — they never swallow caller exceptions.
"""

import logging
import time
from typing import Any, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# ── Exception status code sentinel ────────────────────────────────────────────
_EXCEPTION_STATUS_CODE = -1

# ── Registry entry type alias ─────────────────────────────────────────────────
_RegistryEntry = Tuple[str, str, str]  # (host, path_prefix, dep_name)

# ── Default registry entries ──────────────────────────────────────────────────
#: The 4 contractual API endpoints for the story-spec always-visible HTTP deps.
#: These are the actual production API paths for VoyageAI and Cohere as verified
#: in the story design session (Algorithm 2, Story #680). Used exclusively during
#: server startup wiring (``DependencyRegistryBuilder``) to populate the shared
#: registry — not arbitrary configuration, but spec-mandated dependency URLs.
DEFAULT_REGISTRY_ENTRIES: Tuple[_RegistryEntry, ...] = (
    ("api.voyageai.com", "/v1/embeddings", "voyageai_embed"),
    ("api.voyageai.com", "/v1/rerank", "voyage_rerank"),
    ("api.cohere.com", "/v2/embed", "cohere_embed"),
    ("api.cohere.com", "/v2/rerank", "cohere_rerank"),
)


def _record_sample_if_known(
    dep_name: Optional[str],
    tracker: Any,
    start: float,
    status_code: int,
) -> None:
    """
    Record a latency sample when dep_name is known (not None).

    Shared by sync and async transport wrappers to eliminate logic duplication.

    Args:
        dep_name:    Resolved dependency name, or None if URL is unregistered.
        tracker:     Object with a record_sample(dep_name, latency_ms, status_code) method.
        start:       Wall-clock start time from time.perf_counter().
        status_code: HTTP status code, or _EXCEPTION_STATUS_CODE (-1) on exception.
    """
    if dep_name is not None:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        tracker.record_sample(dep_name, elapsed_ms, status_code)


def build_latency_transport() -> "Optional[LatencyTrackingHTTPXTransport]":
    """Return a LatencyTrackingHTTPXTransport if a tracker singleton is registered.

    Reads the module-level tracker via
    ``dependency_latency_tracker.get_instance()``.  Returns ``None`` when no
    tracker is registered so callers can fall back to a bare httpx.Client.

    This is the canonical wiring point used by embedding and reranking clients
    to attach latency instrumentation at the transport layer.
    """
    from code_indexer.server.services.dependency_latency_tracker import get_instance

    tracker = get_instance()
    if tracker is None:
        return None

    registry = DependencyRegistryBuilder()
    for host, path_prefix, dep_name in DEFAULT_REGISTRY_ENTRIES:
        registry.register(host, path_prefix, dep_name)

    return LatencyTrackingHTTPXTransport(
        wrapped_transport=httpx.HTTPTransport(),
        tracker=tracker,
        registry=registry.build(),
    )


class DependencyRegistry:
    """
    Immutable (host, path_prefix) → dependency_name resolver.

    Constructed from a fixed tuple of entries; never mutated after construction.
    Safe for concurrent read access with no synchronization.

    Use DependencyRegistryBuilder to accumulate entries before freezing.
    """

    def __init__(self, entries: Tuple[_RegistryEntry, ...]) -> None:
        """
        Args:
            entries: Tuple of (host, path_prefix, dep_name) triples.
        """
        self._entries: Tuple[_RegistryEntry, ...] = entries

    def resolve(self, host: str, path: str) -> Optional[str]:
        """
        Return the dependency name for (host, path) using longest-prefix match.

        Args:
            host: Request hostname.
            path: Request path.

        Returns:
            Dependency name string, or None if no registered entry matches.
        """
        best_dep: Optional[str] = None
        best_len = -1
        for entry_host, prefix, dep_name in self._entries:
            if entry_host == host and path.startswith(prefix):
                if len(prefix) > best_len:
                    best_len = len(prefix)
                    best_dep = dep_name
        return best_dep


class DependencyRegistryBuilder:
    """
    Mutable builder that accumulates registry entries before producing an
    immutable DependencyRegistry via build().

    Intended for server startup wiring only — not for use after the registry
    has been shared across request-handling threads.
    """

    def __init__(self) -> None:
        self._entries: List[_RegistryEntry] = []

    def register(self, host: str, path_prefix: str, dep_name: str) -> None:
        """
        Add a (host, path_prefix) → dep_name mapping.

        Args:
            host:        Exact hostname (e.g. "api.voyageai.com").
            path_prefix: Path prefix to match (e.g. "/v1/embeddings").
            dep_name:    Dependency name to record in samples.
        """
        self._entries.append((host, path_prefix, dep_name))

    def build(self) -> DependencyRegistry:
        """Freeze accumulated entries into an immutable DependencyRegistry."""
        return DependencyRegistry(tuple(self._entries))


class LatencyTrackingHTTPXTransport(httpx.HTTPTransport):
    """
    Synchronous HTTPX transport wrapper that records latency samples.

    Delegates every request to ``wrapped_transport``, resolves the dependency
    name via ``registry``, and records a sample via ``tracker``.

    On exception: records status_code=-1 and re-raises.
    For unknown URLs (registry returns None): passes through without recording.
    """

    def __init__(
        self,
        wrapped_transport: httpx.HTTPTransport,
        tracker: Any,
        registry: DependencyRegistry,
    ) -> None:
        # Do NOT call super().__init__() — we delegate to wrapped_transport
        # entirely and must not open a second connection pool.
        self._wrapped = wrapped_transport
        self._tracker = tracker
        self._registry = registry

    @property
    def _pool(self) -> Any:
        """Proxy _pool to the wrapped transport.

        pytest_httpx (and some httpx internals) access _pool to detect proxy
        configuration.  Delegating here makes this wrapper transparent.
        """
        return self._wrapped._pool

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Measure latency and record a sample, then return the response."""
        dep_name = self._registry.resolve(request.url.host, request.url.path)
        start = time.perf_counter()
        status_code = _EXCEPTION_STATUS_CODE
        try:
            response = self._wrapped.handle_request(request)
            status_code = response.status_code
            return response
        except Exception:
            raise
        finally:
            _record_sample_if_known(dep_name, self._tracker, start, status_code)

    def close(self) -> None:
        """Delegate close to the wrapped transport."""
        self._wrapped.close()


class AsyncLatencyTrackingHTTPXTransport(httpx.AsyncHTTPTransport):
    """
    Asynchronous HTTPX transport wrapper that records latency samples.

    Mirrors ``LatencyTrackingHTTPXTransport`` for async clients.
    """

    def __init__(
        self,
        wrapped_transport: httpx.AsyncHTTPTransport,
        tracker: Any,
        registry: DependencyRegistry,
    ) -> None:
        # Do NOT call super().__init__() — we delegate entirely.
        self._wrapped = wrapped_transport
        self._tracker = tracker
        self._registry = registry

    @property
    def _pool(self) -> Any:
        """Proxy _pool to the wrapped transport.

        pytest_httpx (and some httpx internals) access _pool to detect proxy
        configuration.  Delegating here makes this wrapper transparent.
        """
        return self._wrapped._pool

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Measure latency and record a sample, then return the response."""
        dep_name = self._registry.resolve(request.url.host, request.url.path)
        start = time.perf_counter()
        status_code = _EXCEPTION_STATUS_CODE
        try:
            response = await self._wrapped.handle_async_request(request)
            status_code = response.status_code
            return response
        except Exception:
            raise
        finally:
            _record_sample_if_known(dep_name, self._tracker, start, status_code)

    async def aclose(self) -> None:
        """Delegate aclose to the wrapped transport."""
        await self._wrapped.aclose()
