"""Shared fixtures for cli_search_funnel test suite (Story #693 -- Epic #689).

All service wiring, HTTP boundary patching, and result-shape builders live here.
Test files consume fixtures by name; no test method contains inline patching
of ProviderHealthMonitor.get_instance, get_config_service, or reranker _post.

Anti-mock compliance (MESSI Rule 1):
  - ProviderHealthMonitor: real instance (file persistence via tmp_path, Story #691).
  - CliRerankConfigService: real instance wrapping a real GlobalCliConfig.
  - Module-level service lookups (get_config_service, get_instance) must be patched
    because the server reranker clients resolve them via module-level indirection
    (code_indexer.server.services.config_service.get_config_service and
    ProviderHealthMonitor.get_instance).  These are the ONLY test seams available
    in the server code that Story #693 must not modify.  The patch boundary is
    explicitly documented in _wire_services() docstring.
  - HTTP boundary patched only at VoyageRerankerClient._post /
    CohereRerankerClient._post -- all orchestration code runs for real.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import httpx
import pytest

import code_indexer.server.clients.reranker_clients as rc_module
from code_indexer.config_global import GlobalCliConfig, RerankSettings
from code_indexer.services.cli_rerank_config_shim import CliRerankConfigService
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

# Sentinel strings -- not real API credentials; used only to satisfy the shim's
# "key is present" check so the reranker model strings are returned as non-empty.
SENTINEL_VOYAGE_KEY = "sentinel-voyage-key-for-testing"
SENTINEL_COHERE_KEY = "sentinel-cohere-key-for-testing"


# ---------------------------------------------------------------------------
# Result shape builders (imported by test modules)
# ---------------------------------------------------------------------------


def make_semantic(
    *,
    path: str = "src/foo.py",
    line_start: int = 1,
    line_end: int = 10,
    score: float = 0.9,
    content: str = "def foo(): pass",
    language: str = "python",
    file_last_modified: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Build a semantic search result dict matching cli.py output shape.

    file_last_modified is a genuine payload-level field in real semantic results
    (returned inside the 'payload' dict by the server).  It is accepted as an
    explicit parameter here so callers can set it and assert o["payload"]["file_last_modified"]
    correctly.  Other unknown fields passed via **extra land at the top-level result
    dict (e.g. staleness, custom_tag) as per the actual server response shape.
    """
    payload: Dict[str, Any] = {
        "path": path,
        "line_start": line_start,
        "line_end": line_end,
        "content": content,
        "language": language,
    }
    if file_last_modified is not None:
        payload["file_last_modified"] = file_last_modified
    result: Dict[str, Any] = {"score": score, "payload": payload}
    result.update(extra)
    return result


def make_fts(
    *,
    path: str = "src/bar.py",
    line: int = 5,
    column: int = 0,
    match_text: str = "bar",
    snippet: str = "def bar(): pass",
    language: str = "python",
    **extra: Any,
) -> Dict[str, Any]:
    """Build an FTS search result dict matching TantivyIndexManager output shape."""
    result: Dict[str, Any] = {
        "path": path,
        "line": line,
        "column": column,
        "match_text": match_text,
        "snippet": snippet,
        "language": language,
    }
    result.update(extra)
    return result


# ---------------------------------------------------------------------------
# Config builder
# ---------------------------------------------------------------------------


def make_global_config(
    overfetch: int = 3,
    preferred_order: Optional[List[str]] = None,
) -> GlobalCliConfig:
    return GlobalCliConfig(
        rerank=RerankSettings(
            voyage_reranker_model="rerank-2.5",
            cohere_reranker_model="rerank-v3.5",
            overfetch_multiplier=overfetch,
            preferred_vendor_order=(
                preferred_order if preferred_order is not None else ["voyage", "cohere"]
            ),
        )
    )


# ---------------------------------------------------------------------------
# Voyage API response builders (pure functions, no side effects)
# ---------------------------------------------------------------------------


def build_voyage_identity_response(body: Dict[str, Any]) -> MagicMock:
    """Voyage HTTP response preserving original document order.

    Scores: doc[0]=1.0, doc[1]=0.9, ... decreasing.
    Used to verify truncation and field preservation without changing order.
    """
    top_k = body.get("top_k", len(body["documents"]))
    n = min(top_k, len(body["documents"]))
    data = [{"index": i, "relevance_score": 1.0 - i * 0.1} for i in range(n)]
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = {"data": data}
    resp.raise_for_status = MagicMock()
    return resp


def build_voyage_reversed_response(body: Dict[str, Any]) -> MagicMock:
    """Voyage HTTP response scoring last document highest (full order reversal).

    Used to verify the funnel correctly applies reranker ordering.

    i=0 maps to the last document (index=len(docs)-1) and must receive the
    highest score so it sorts first after descending sort.  Score formula:
    float(n-i)/n gives i=0 -> score=1.0 (highest), i=1 -> (n-1)/n, descending.
    Previous formula float(i+1)/len(docs) was inverted: i=0 got the lowest
    score (1/n) so doc[1] sorted first instead of doc[len(docs)-1].
    """
    docs = body["documents"]
    top_k = body.get("top_k", len(docs))
    n = min(top_k, len(docs))
    # i=0: index=last-doc, score=n/n=1.0 (highest); i=1: second-last, (n-1)/n; ...
    data = [
        {"index": len(docs) - 1 - i, "relevance_score": float(n - i) / n}
        for i in range(n)
    ]
    data.sort(key=lambda x: x["relevance_score"], reverse=True)
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = {"data": data}
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Service wiring helper
# ---------------------------------------------------------------------------


def _wire_services(
    monitor: ProviderHealthMonitor,
    config: CliRerankConfigService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch the two module-level service lookups used by the server reranker clients.

    WHY module-level patching is necessary here:
      VoyageRerankerClient and CohereRerankerClient resolve their dependencies via:
        - code_indexer.server.clients.reranker_clients.get_config_service()
        - code_indexer.services.provider_health_monitor.ProviderHealthMonitor.get_instance()
      Story #693 must not modify server code, so these module-level entry points are
      the only available test seams.  The patches supply REAL objects (not MagicMocks):
        - monitor: a real ProviderHealthMonitor with file persistence
        - config: a real CliRerankConfigService wrapping a real GlobalCliConfig

    Called exactly once per composite fixture; never repeated inline in test methods.
    """
    monkeypatch.setattr(
        ProviderHealthMonitor, "get_instance", staticmethod(lambda: monitor)
    )
    monkeypatch.setattr(rc_module, "get_config_service", lambda: config)


# ---------------------------------------------------------------------------
# Service stack container
# ---------------------------------------------------------------------------


class ServiceStack:
    """Container returned by composite fixtures exposing .monitor and .config."""

    def __init__(
        self,
        monitor: ProviderHealthMonitor,
        config: CliRerankConfigService,
    ) -> None:
        self.monitor = monitor
        self.config = config


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def real_monitor(tmp_path: Path) -> ProviderHealthMonitor:
    """Real ProviderHealthMonitor isolated per test (reset + file persistence)."""
    ProviderHealthMonitor.reset_instance()
    monitor = ProviderHealthMonitor(persistence_path=tmp_path / "health.json")
    yield monitor
    ProviderHealthMonitor.reset_instance()


@pytest.fixture()
def no_key_config(monkeypatch: pytest.MonkeyPatch) -> CliRerankConfigService:
    """Shim with no API keys.

    Explicitly clears both env vars so real credentials from the test runner
    environment cannot leak in and accidentally enable reranking.
    """
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CO_API_KEY", raising=False)
    return CliRerankConfigService(make_global_config())


@pytest.fixture()
def voyage_config(monkeypatch: pytest.MonkeyPatch) -> CliRerankConfigService:
    """Shim with VOYAGE_API_KEY sentinel set; Cohere key explicitly absent."""
    monkeypatch.setenv("VOYAGE_API_KEY", SENTINEL_VOYAGE_KEY)
    monkeypatch.delenv("CO_API_KEY", raising=False)
    return CliRerankConfigService(make_global_config())


@pytest.fixture()
def both_key_config(monkeypatch: pytest.MonkeyPatch) -> CliRerankConfigService:
    """Shim with both provider sentinel keys set."""
    monkeypatch.setenv("VOYAGE_API_KEY", SENTINEL_VOYAGE_KEY)
    monkeypatch.setenv("CO_API_KEY", SENTINEL_COHERE_KEY)
    return CliRerankConfigService(make_global_config())


# ---------------------------------------------------------------------------
# Composite service stack fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def wire_voyage_stack(
    real_monitor: ProviderHealthMonitor,
    voyage_config: CliRerankConfigService,
    monkeypatch: pytest.MonkeyPatch,
) -> ServiceStack:
    """Real monitor + voyage_config wired via _wire_services (called once)."""
    _wire_services(real_monitor, voyage_config, monkeypatch)
    return ServiceStack(real_monitor, voyage_config)


@pytest.fixture()
def wire_both_key_stack(
    real_monitor: ProviderHealthMonitor,
    both_key_config: CliRerankConfigService,
    monkeypatch: pytest.MonkeyPatch,
) -> ServiceStack:
    """Real monitor + both_key_config wired via _wire_services (called once)."""
    _wire_services(real_monitor, both_key_config, monkeypatch)
    return ServiceStack(real_monitor, both_key_config)


# ---------------------------------------------------------------------------
# HTTP boundary patch fixtures (one per distinct _post behaviour)
# ---------------------------------------------------------------------------


@pytest.fixture()
def voyage_identity_patched(
    wire_voyage_stack: ServiceStack,
    monkeypatch: pytest.MonkeyPatch,
) -> ServiceStack:
    """wire_voyage_stack with VoyageRerankerClient._post returning identity order."""
    monkeypatch.setattr(
        rc_module.VoyageRerankerClient,
        "_post",
        lambda self_c, b: build_voyage_identity_response(b),
    )
    return wire_voyage_stack


@pytest.fixture()
def voyage_reversed_patched(
    wire_voyage_stack: ServiceStack,
    monkeypatch: pytest.MonkeyPatch,
) -> ServiceStack:
    """wire_voyage_stack with VoyageRerankerClient._post returning reversed order."""
    monkeypatch.setattr(
        rc_module.VoyageRerankerClient,
        "_post",
        lambda self_c, b: build_voyage_reversed_response(b),
    )
    return wire_voyage_stack


@pytest.fixture()
def both_rerankers_failing_patched(
    wire_both_key_stack: ServiceStack,
    monkeypatch: pytest.MonkeyPatch,
) -> ServiceStack:
    """wire_both_key_stack with both reranker _post methods raising ConnectError."""
    monkeypatch.setattr(
        rc_module.VoyageRerankerClient,
        "_post",
        lambda self_c, b: (_ for _ in ()).throw(httpx.ConnectError("voyage down")),
    )
    monkeypatch.setattr(
        rc_module.CohereRerankerClient,
        "_post",
        lambda self_c, b: (_ for _ in ()).throw(httpx.ConnectError("cohere down")),
    )
    return wire_both_key_stack


@pytest.fixture()
def voyage_counting_patched(
    wire_voyage_stack: ServiceStack,
    monkeypatch: pytest.MonkeyPatch,
) -> Tuple[ServiceStack, Dict[str, int]]:
    """wire_voyage_stack with _post that counts invocations.

    Returns (ServiceStack, call_count) where call_count["n"] increments per call.
    Used in hybrid-mode tests to verify independent per-sublist rerank calls.
    """
    call_count: Dict[str, int] = {"n": 0}

    def counting_post(self_client, body: Dict[str, Any]) -> MagicMock:
        call_count["n"] += 1
        return build_voyage_identity_response(body)

    monkeypatch.setattr(rc_module.VoyageRerankerClient, "_post", counting_post)
    return wire_voyage_stack, call_count
