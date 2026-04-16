"""
Unit tests for LatencyTrackingHTTPXTransport and AsyncLatencyTrackingHTTPXTransport.

Story #680: External Dependency Latency Observability

Tests written FIRST following TDD methodology.

Covers:
- DependencyRegistry resolves exactly 4 known entries from DEFAULT_REGISTRY_ENTRIES
- Unknown host derived from registry data; unknown path verified at module load
  to not match any registered prefix for the chosen host
- Longest-prefix-wins using a synthetic local-only test host
- Sync transport: success, exception (-1), server-error status, unknown URL (no sample)
- Async transport: success, exception (-1), unknown URL (no sample)
"""

from typing import List

import httpx
import pytest

from code_indexer.server.services.latency_tracking_httpx_transport import (
    DEFAULT_REGISTRY_ENTRIES,
    AsyncLatencyTrackingHTTPXTransport,
    DependencyRegistry,
    DependencyRegistryBuilder,
    LatencyTrackingHTTPXTransport,
)

# ── Named constants: expected registry size ───────────────────────────────────
EXPECTED_DEFAULT_ENTRY_COUNT = 4

# ── Named constants: status codes ─────────────────────────────────────────────
STATUS_OK = 200
STATUS_SERVER_ERROR = 500
STATUS_EXCEPTION = -1

# ── Named constants: latency ──────────────────────────────────────────────────
MIN_LATENCY_MS = 0.0

# ── Named constants: index / count ────────────────────────────────────────────
FIRST_SAMPLE_INDEX = 0
EXPECTED_ONE_SAMPLE = 1
EXPECTED_ZERO_SAMPLES = 0

# ── Named constants: sample fields ────────────────────────────────────────────
FIELD_DEP_NAME = "dependency_name"
FIELD_STATUS_CODE = "status_code"
FIELD_LATENCY_MS = "latency_ms"

# ── Named constants: synthetic local-test host for longest-prefix test ─────────
# NOT a real endpoint — used only to verify prefix-matching logic in isolation.
_SYNTHETIC_TEST_HOST = "synthetic.test.local"
_SYNTHETIC_PATH_SHORT = "/v1"
_SYNTHETIC_PATH_LONG = "/v1/embeddings"
_SYNTHETIC_DEP_SHORT = "synthetic_short"
_SYNTHETIC_DEP_LONG = "synthetic_long"

# ── Derive test data from the module's own DEFAULT_REGISTRY_ENTRIES ────────────
_FIRST_HOST, _FIRST_PATH, _FIRST_DEP = DEFAULT_REGISTRY_ENTRIES[0]
_KNOWN_URL = f"https://{_FIRST_HOST}{_FIRST_PATH}"

# Unknown host: appending ".invalid" to a known host guarantees a different value
# that cannot appear in any registered entry.
_UNKNOWN_HOST = f"{_FIRST_HOST}.invalid"

# Unknown path: "/unregistered/endpoint" is chosen as the candidate.
# The string-prefix invariant is verified at module load with the assertions below:
#   (a) The candidate does not start with any registered prefix for _FIRST_HOST —
#       so a longest-prefix resolver cannot match it to any registered dependency.
#   (b) No registered prefix for _FIRST_HOST starts with the candidate —
#       so the candidate is not accidentally a sub-path of a registered one.
# Both conditions together guarantee that the registry returns None for this path.
_UNKNOWN_PATH_CANDIDATE = "/unregistered/endpoint"
_FIRST_HOST_PREFIXES = [
    path for host, path, _ in DEFAULT_REGISTRY_ENTRIES if host == _FIRST_HOST
]
assert all(not _UNKNOWN_PATH_CANDIDATE.startswith(p) for p in _FIRST_HOST_PREFIXES), (
    f"Test invariant (a) broken: {_UNKNOWN_PATH_CANDIDATE!r} starts with a registered "
    f"prefix in {_FIRST_HOST_PREFIXES!r} — choose a different candidate."
)
assert all(not p.startswith(_UNKNOWN_PATH_CANDIDATE) for p in _FIRST_HOST_PREFIXES), (
    f"Test invariant (b) broken: a registered prefix in {_FIRST_HOST_PREFIXES!r} "
    f"starts with {_UNKNOWN_PATH_CANDIDATE!r} — choose a different candidate."
)
_UNKNOWN_PATH_SEGMENT = _UNKNOWN_PATH_CANDIDATE
_UNKNOWN_URL = f"https://{_UNKNOWN_HOST}{_UNKNOWN_PATH_SEGMENT}"


# ── Minimal tracker stub ──────────────────────────────────────────────────────


class _RecordingTracker:
    """Minimal tracker stub that records samples into a list without I/O."""

    def __init__(self) -> None:
        self.samples: List[dict] = []

    def record_sample(
        self, dependency_name: str, latency_ms: float, status_code: int
    ) -> None:
        self.samples.append(
            {
                FIELD_DEP_NAME: dependency_name,
                FIELD_LATENCY_MS: latency_ms,
                FIELD_STATUS_CODE: status_code,
            }
        )


# ── Minimal HTTPX transport stubs ────────────────────────────────────────────


class _SuccessTransport(httpx.HTTPTransport):
    """Sync stub: returns HTTP 200 without a real network call."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(STATUS_OK, request=request)


class _ErrorTransport(httpx.HTTPTransport):
    """Sync stub: raises RuntimeError without a real network call."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise RuntimeError("simulated network error")


class _ServerErrorTransport(httpx.HTTPTransport):
    """Sync stub: returns HTTP 500."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(STATUS_SERVER_ERROR, request=request)


class _AsyncSuccessTransport(httpx.AsyncHTTPTransport):
    """Async stub: returns HTTP 200."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(STATUS_OK, request=request)


class _AsyncErrorTransport(httpx.AsyncHTTPTransport):
    """Async stub: raises RuntimeError."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise RuntimeError("simulated async network error")


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def registry() -> DependencyRegistry:
    """DependencyRegistry built from the module's own DEFAULT_REGISTRY_ENTRIES."""
    builder = DependencyRegistryBuilder()
    for host, path_prefix, dep_name in DEFAULT_REGISTRY_ENTRIES:
        builder.register(host, path_prefix, dep_name)
    return builder.build()


@pytest.fixture
def tracker() -> _RecordingTracker:
    """Recording tracker stub."""
    return _RecordingTracker()


# ── Tests: DependencyRegistry ─────────────────────────────────────────────────


class TestDependencyRegistry:
    """Tests for DependencyRegistry URL resolution."""

    def test_default_registry_has_exactly_four_entries(self) -> None:
        """DEFAULT_REGISTRY_ENTRIES contains exactly 4 mappings as declared in the spec."""
        assert len(DEFAULT_REGISTRY_ENTRIES) == EXPECTED_DEFAULT_ENTRY_COUNT

    @pytest.mark.parametrize("host,path,expected_dep", DEFAULT_REGISTRY_ENTRIES)
    def test_resolves_all_known_entries(
        self, registry, host: str, path: str, expected_dep: str
    ) -> None:
        """Registry resolves every entry in DEFAULT_REGISTRY_ENTRIES to the correct dep name."""
        assert registry.resolve(host, path) == expected_dep

    def test_returns_none_for_unknown_host(self, registry) -> None:
        """Registry returns None for a host not present in the registry."""
        assert registry.resolve(_UNKNOWN_HOST, _FIRST_PATH) is None

    def test_returns_none_for_unknown_path_on_known_host(self, registry) -> None:
        """Registry returns None for a path that does not match any registered prefix."""
        assert registry.resolve(_FIRST_HOST, _UNKNOWN_PATH_SEGMENT) is None

    def test_longest_prefix_wins(self) -> None:
        """Longest matching path prefix wins over a shorter one (synthetic host only)."""
        builder = DependencyRegistryBuilder()
        builder.register(
            _SYNTHETIC_TEST_HOST, _SYNTHETIC_PATH_SHORT, _SYNTHETIC_DEP_SHORT
        )
        builder.register(
            _SYNTHETIC_TEST_HOST, _SYNTHETIC_PATH_LONG, _SYNTHETIC_DEP_LONG
        )
        reg = builder.build()
        assert (
            reg.resolve(_SYNTHETIC_TEST_HOST, _SYNTHETIC_PATH_LONG + "/batch")
            == _SYNTHETIC_DEP_LONG
        )


# ── Tests: sync transport ─────────────────────────────────────────────────────


class TestLatencyTrackingHTTPXTransport:
    """Tests for the synchronous LatencyTrackingHTTPXTransport."""

    def test_records_sample_on_successful_request(self, registry, tracker) -> None:
        """Transport records dep_name, latency_ms>=0, correct status_code on success."""
        transport = LatencyTrackingHTTPXTransport(
            wrapped_transport=_SuccessTransport(),
            tracker=tracker,
            registry=registry,
        )
        response = transport.handle_request(httpx.Request("POST", _KNOWN_URL))

        assert response.status_code == STATUS_OK
        assert len(tracker.samples) == EXPECTED_ONE_SAMPLE
        sample = tracker.samples[FIRST_SAMPLE_INDEX]
        assert sample[FIELD_DEP_NAME] == _FIRST_DEP
        assert sample[FIELD_STATUS_CODE] == STATUS_OK
        assert sample[FIELD_LATENCY_MS] >= MIN_LATENCY_MS

    def test_records_status_minus_one_on_exception_and_reraises(
        self, registry, tracker
    ) -> None:
        """Transport records status_code=-1 when wrapped transport raises, then re-raises."""
        transport = LatencyTrackingHTTPXTransport(
            wrapped_transport=_ErrorTransport(),
            tracker=tracker,
            registry=registry,
        )
        with pytest.raises(RuntimeError):
            transport.handle_request(httpx.Request("POST", _KNOWN_URL))

        assert len(tracker.samples) == EXPECTED_ONE_SAMPLE
        assert (
            tracker.samples[FIRST_SAMPLE_INDEX][FIELD_STATUS_CODE] == STATUS_EXCEPTION
        )

    def test_records_actual_status_code_on_server_error(
        self, registry, tracker
    ) -> None:
        """Transport records the actual HTTP status code (e.g. 500) on server error."""
        transport = LatencyTrackingHTTPXTransport(
            wrapped_transport=_ServerErrorTransport(),
            tracker=tracker,
            registry=registry,
        )
        transport.handle_request(httpx.Request("POST", _KNOWN_URL))

        assert (
            tracker.samples[FIRST_SAMPLE_INDEX][FIELD_STATUS_CODE]
            == STATUS_SERVER_ERROR
        )

    def test_no_sample_recorded_for_unknown_url(self, registry, tracker) -> None:
        """Transport does not record a sample when URL host is not in the registry."""
        transport = LatencyTrackingHTTPXTransport(
            wrapped_transport=_SuccessTransport(),
            tracker=tracker,
            registry=registry,
        )
        transport.handle_request(httpx.Request("GET", _UNKNOWN_URL))

        assert len(tracker.samples) == EXPECTED_ZERO_SAMPLES


# ── Tests: async transport ────────────────────────────────────────────────────


class TestAsyncLatencyTrackingHTTPXTransport:
    """Tests for the asynchronous AsyncLatencyTrackingHTTPXTransport."""

    @pytest.mark.asyncio
    async def test_records_sample_on_successful_async_request(
        self, registry, tracker
    ) -> None:
        """Async transport records dep_name, latency_ms>=0, correct status_code on success."""
        transport = AsyncLatencyTrackingHTTPXTransport(
            wrapped_transport=_AsyncSuccessTransport(),
            tracker=tracker,
            registry=registry,
        )
        response = await transport.handle_async_request(
            httpx.Request("POST", _KNOWN_URL)
        )

        assert response.status_code == STATUS_OK
        assert len(tracker.samples) == EXPECTED_ONE_SAMPLE
        sample = tracker.samples[FIRST_SAMPLE_INDEX]
        assert sample[FIELD_DEP_NAME] == _FIRST_DEP
        assert sample[FIELD_STATUS_CODE] == STATUS_OK
        assert sample[FIELD_LATENCY_MS] >= MIN_LATENCY_MS

    @pytest.mark.asyncio
    async def test_records_status_minus_one_on_async_exception_and_reraises(
        self, registry, tracker
    ) -> None:
        """Async transport records status_code=-1 on exception and re-raises."""
        transport = AsyncLatencyTrackingHTTPXTransport(
            wrapped_transport=_AsyncErrorTransport(),
            tracker=tracker,
            registry=registry,
        )
        with pytest.raises(RuntimeError):
            await transport.handle_async_request(httpx.Request("POST", _KNOWN_URL))

        assert len(tracker.samples) == EXPECTED_ONE_SAMPLE
        assert (
            tracker.samples[FIRST_SAMPLE_INDEX][FIELD_STATUS_CODE] == STATUS_EXCEPTION
        )

    @pytest.mark.asyncio
    async def test_no_sample_recorded_for_unknown_async_url(
        self, registry, tracker
    ) -> None:
        """Async transport does not record a sample when URL is not in the registry."""
        transport = AsyncLatencyTrackingHTTPXTransport(
            wrapped_transport=_AsyncSuccessTransport(),
            tracker=tracker,
            registry=registry,
        )
        await transport.handle_async_request(httpx.Request("GET", _UNKNOWN_URL))

        assert len(tracker.samples) == EXPECTED_ZERO_SAMPLES


# ── Tests: DEFAULT_REGISTRY_ENTRIES correctness ───────────────────────────────

_COHERE_ENTRIES = [
    (host, path, dep) for host, path, dep in DEFAULT_REGISTRY_ENTRIES if "cohere" in dep
]
_VOYAGE_ENTRIES = [
    (host, path, dep)
    for host, path, dep in DEFAULT_REGISTRY_ENTRIES
    if "voyage" in dep or "voyageai" in dep
]


class TestDefaultRegistryEntriesCorrectness:
    """Verify DEFAULT_REGISTRY_ENTRIES matches the actual API endpoints used by clients."""

    def test_cohere_entries_use_api_cohere_com_host(self) -> None:
        """Cohere registry entries must use api.cohere.com (not api.cohere.ai)."""
        for host, _path, _dep in _COHERE_ENTRIES:
            assert host == "api.cohere.com", (
                f"Cohere entry host is {host!r}; expected 'api.cohere.com'"
            )

    def test_cohere_embed_entry_uses_v2_path(self) -> None:
        """Cohere embed entry must use /v2/embed (matches CohereEmbeddingProvider config)."""
        embed_paths = [path for _host, path, dep in _COHERE_ENTRIES if "embed" in dep]
        assert embed_paths, "No Cohere embed entry found in DEFAULT_REGISTRY_ENTRIES"
        for path in embed_paths:
            assert path.startswith("/v2/"), (
                f"Cohere embed path is {path!r}; expected /v2/ prefix"
            )

    def test_cohere_rerank_entry_uses_v2_path(self) -> None:
        """Cohere rerank entry must use /v2/rerank (matches CohereRerankerClient constant)."""
        rerank_paths = [path for _host, path, dep in _COHERE_ENTRIES if "rerank" in dep]
        assert rerank_paths, "No Cohere rerank entry found in DEFAULT_REGISTRY_ENTRIES"
        for path in rerank_paths:
            assert path.startswith("/v2/"), (
                f"Cohere rerank path is {path!r}; expected /v2/ prefix"
            )

    def test_voyageai_entries_use_api_voyageai_com_host(self) -> None:
        """VoyageAI registry entries must use api.voyageai.com."""
        for host, _path, _dep in _VOYAGE_ENTRIES:
            assert host == "api.voyageai.com", (
                f"VoyageAI entry host is {host!r}; expected 'api.voyageai.com'"
            )


# ── Tests: build_latency_transport() helper ───────────────────────────────────


def _reset_tracker_singleton() -> None:
    """Clear the module-level tracker singleton between tests."""
    from code_indexer.server.services.dependency_latency_tracker import set_instance

    set_instance(None)


class TestBuildLatencyTransport:
    """Tests for the build_latency_transport() convenience helper."""

    def setup_method(self) -> None:
        _reset_tracker_singleton()

    def teardown_method(self) -> None:
        _reset_tracker_singleton()

    def test_returns_none_when_no_tracker_registered(self) -> None:
        """build_latency_transport() returns None when no tracker is registered."""
        from code_indexer.server.services.latency_tracking_httpx_transport import (
            build_latency_transport,
        )

        assert build_latency_transport() is None

    def test_returns_transport_when_tracker_registered(self, tracker) -> None:
        """build_latency_transport() returns LatencyTrackingHTTPXTransport when tracker set."""
        from code_indexer.server.services.dependency_latency_tracker import (
            set_instance,
        )
        from code_indexer.server.services.latency_tracking_httpx_transport import (
            LatencyTrackingHTTPXTransport,
            build_latency_transport,
        )

        set_instance(tracker)
        transport = build_latency_transport()
        assert isinstance(transport, LatencyTrackingHTTPXTransport)

    def test_returned_transport_records_samples_for_known_url(self, tracker) -> None:
        """Transport from build_latency_transport() records a sample for a known URL."""
        from code_indexer.server.services.dependency_latency_tracker import (
            set_instance,
        )
        from code_indexer.server.services.latency_tracking_httpx_transport import (
            build_latency_transport,
        )

        set_instance(tracker)
        transport = build_latency_transport()
        assert transport is not None

        voyage_url = f"https://{_FIRST_HOST}{_FIRST_PATH}"
        transport._wrapped = _SuccessTransport()
        transport.handle_request(httpx.Request("POST", voyage_url))

        assert len(tracker.samples) == EXPECTED_ONE_SAMPLE
        assert tracker.samples[FIRST_SAMPLE_INDEX][FIELD_DEP_NAME] == _FIRST_DEP
