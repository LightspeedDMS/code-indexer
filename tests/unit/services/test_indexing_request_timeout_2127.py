"""Bug #2127: an indexing embedding request waits long enough for a slow
provider response instead of abandoning it and resending the same batch.

A provider slow period put every indexing response just past the 30 s
request timeout: each batch was abandoned and resent up to four times, and
an abandoned request may still be billed. The indexing path now waits
``INDEXING_TIMEOUT_MULTIPLIER`` times the configured provider timeout; the
QUERY path keeps the configured timeout unchanged.

Every test scales the configured timeout down through the real provider
config (``timeout`` = 1 s) and holds each fake-provider response for
``_HOLD_SECONDS`` (past the configured timeout, inside the indexing budget).
No real provider call is possible: the Voyage fake is
scripts/analysis/reembed_repro/fake_voyage_server.py (127.0.0.1, sentinel
key), the other stand-ins below bind 127.0.0.1 only, and real-CLI runs go
through the network guard of tests/utils/cidx_fake_provider_repo.py.
"""

from __future__ import annotations

import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterator

import httpx
import pytest

from code_indexer.config import CohereConfig, VoyageAIConfig
from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
from code_indexer.services.cohere_multimodal import CohereMultimodalClient
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor
from code_indexer.services.provider_request_timeout import embedding_request_timeout
from code_indexer.services.voyage_ai import VoyageAIClient
from code_indexer.services.voyage_multimodal import VoyageMultimodalClient
from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SENTINEL_KEY = "issue-2127-fake-key"

#: The configured provider request timeout every test runs with (seconds).
_CONFIG_TIMEOUT_SECONDS = 1
#: How long the fake holds every response: past the configured timeout,
#: well inside the indexing budget (4 x the configured timeout).
_HOLD_SECONDS = 2.0
_MAX_RETRIES = 3
#: Bound of the wait for the fake's handler threads to record every outcome.
_SETTLE_SECONDS = 30
#: Dimensions of the voyage-context-4 stand-in embeddings.
_DIMENSIONS = 1024
#: Dimensions of the embed-v4.0 stand-in embeddings.
_COHERE_DIMENSIONS = 1536
_STUB_VALUE = 0.1
_SERVER_JOIN_SECONDS = 10

pytestmark = pytest.mark.timeout(240)


@pytest.fixture(autouse=True)
def _isolated_health_monitor() -> Iterator[None]:
    """The timeout failures these tests provoke are recorded on the
    process-wide provider health monitor; start and leave it clean so no
    later test sees a sinbinned provider."""
    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture
def fake_voyage() -> Iterator[object]:
    # The shared helper stops the fake and asserts no ledger violation.
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


class _HeldJsonServer:
    """Local stand-in for a provider endpoint (127.0.0.1 only): answers every
    POST with ``respond(payload)`` after holding it ``hold`` seconds, and
    counts the requests it received."""

    def __init__(self, respond: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        self.hold = 0.0
        self.requests = 0
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with owner._lock:
                    owner.requests += 1
                owner._stopping.wait(owner.hold)
                body = json.dumps(respond(json.loads(raw))).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except ConnectionError:
                    self.close_connection = True  # the client gave up

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host!s}:{port}/"

    def stop(self) -> None:
        self._stopping.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=_SERVER_JOIN_SECONDS)


@contextlib.contextmanager
def _held_server(
    respond: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Iterator[_HeldJsonServer]:
    server = _HeldJsonServer(respond)
    try:
        yield server
    finally:
        server.stop()


class _RedirectingFactory:
    """Client factory for an endpoint the client hard-codes (the vendor's
    contextualized-embeddings URL): a REAL httpx client built with the
    caller's kwargs, every ``post`` sent to the local stand-in instead."""

    def __init__(self, target: str) -> None:
        self._target = target

    def create_sync_client(self, **kwargs: Any) -> Any:
        kwargs.pop("pooled", None)
        kwargs.pop("transport", None)
        client = httpx.Client(**kwargs)
        target = self._target

        class _Redirected:
            def __enter__(self) -> "_Redirected":
                return self

            def __exit__(self, *exc: object) -> None:
                client.close()

            def post(self, url: str, **post_kwargs: Any) -> httpx.Response:
                return client.post(target, **post_kwargs)

        return _Redirected()


def _contextualized_reply(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "object": "list",
                "index": i,
                "data": [
                    {
                        "object": "embedding",
                        "index": j,
                        "embedding": [_STUB_VALUE] * _DIMENSIONS,
                    }
                    for j, _chunk in enumerate(document)
                ],
            }
            for i, document in enumerate(payload["inputs"])
        ],
        "model": payload["model"],
    }


def _voyage_config(endpoint: str, **overrides: Any) -> VoyageAIConfig:
    values: Dict[str, Any] = {
        "api_endpoint": endpoint,
        "timeout": _CONFIG_TIMEOUT_SECONDS,
        "max_retries": _MAX_RETRIES,
        "retry_delay": 0.0,
    }
    values.update(overrides)
    return VoyageAIConfig(**values)


def _timeout_in_chain(exc: BaseException) -> bool:
    current: "BaseException | None" = exc
    while current is not None:
        if isinstance(current, httpx.TimeoutException):
            return True
        current = current.__cause__ or current.__context__
    return False


@pytest.mark.slow
def test_cli_index_waits_for_slow_response_and_sends_each_input_once(
    tmp_path: Path, fake_voyage
) -> None:
    """A real ``cidx index`` whose provider answers after the configured
    timeout receives the answer: every input reaches the provider exactly
    once (no abandoned-and-resent batch) and the run succeeds."""
    repo = FakeProviderRepo(tmp_path, fake_voyage, _SENTINEL_KEY)
    config_path = repo.repo / ".code-indexer" / "config.json"
    config = json.loads(config_path.read_text())
    config["voyage_ai"].update(
        {
            "timeout": _CONFIG_TIMEOUT_SECONDS,
            "max_retries": _MAX_RETRIES,
            "retry_delay": 0.0,
        }
    )
    config_path.write_text(json.dumps(config))
    repo.write_files("slow", 2, lines=10)
    fake_voyage.ledger.begin_run("slow", hold_seconds=_HOLD_SECONDS)

    proc = repo.cidx(*repo.index_args())

    sent = fake_voyage.ledger.run_key_counts("slow")
    assert sent, "the run sent nothing to the provider"
    # The fake records each outcome AFTER the bytes reach the client, so wait
    # for every request to settle before reading them. One abandoned file of
    # two still exits 0 without a resend: assert no request was abandoned.
    assert fake_voyage.ledger.wait_settled("slow", timeout=_SETTLE_SECONDS)
    abandoned = [r.request_id for r in fake_voyage.ledger.requests("slow") if r.aborted]
    assert abandoned == [], f"requests abandoned by the client: {abandoned}"
    resent = {key: count for key, count in sent.items() if count != 1}
    assert resent == {}, (
        f"{len(resent)} of {len(sent)} inputs were sent more than once: "
        f"{sorted(resent.values())}"
    )
    assert proc.returncode == 0, proc.stderr[-3000:]


def test_voyage_query_embedding_keeps_configured_timeout(
    fake_voyage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The QUERY path is untouched: a query embedding still gives up at the
    configured timeout (one attempt), so a response held past it is not
    waited for."""
    monkeypatch.setenv("VOYAGE_API_KEY", _SENTINEL_KEY)
    client = VoyageAIClient(_voyage_config(f"{fake_voyage.base_url}/v1/embeddings"))
    fake_voyage.ledger.begin_run("query", hold_seconds=_HOLD_SECONDS)

    with pytest.raises(Exception) as raised:
        client.get_embedding("find the slow function", embedding_purpose="query")

    assert _timeout_in_chain(raised.value), repr(raised.value)
    assert len(fake_voyage.ledger.requests("query")) == 1


def test_voyage_indexing_timeout_not_shortened_by_pooled_client_built_by_query(
    fake_voyage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pooled keep-alive client keeps the timeout of the request that
    built it. A query call builds it first (configured timeout); a later
    indexing batch through the same client must still get the indexing
    budget: one request, answered."""
    monkeypatch.setenv("VOYAGE_API_KEY", _SENTINEL_KEY)
    client = VoyageAIClient(_voyage_config(f"{fake_voyage.base_url}/v1/embeddings"))
    fake_voyage.ledger.begin_run("query-first", hold_seconds=_HOLD_SECONDS)
    with pytest.raises(Exception):
        client.get_embedding("build the pooled client", embedding_purpose="query")

    fake_voyage.ledger.begin_run("index-after", hold_seconds=_HOLD_SECONDS)
    embeddings = client.get_embeddings_batch(["def a(): pass", "def b(): pass"])

    assert len(embeddings) == 2
    requests = fake_voyage.ledger.requests("index-after")
    assert len(requests) == 1, f"{len(requests)} requests for one batch"


def test_voyage_query_timeout_not_lengthened_by_pooled_client_built_by_indexing(
    fake_voyage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverse order: an indexing batch builds the pooled client first
    (indexing budget); a later query through the same client must still
    give up at its own configured timeout."""
    monkeypatch.setenv("VOYAGE_API_KEY", _SENTINEL_KEY)
    client = VoyageAIClient(_voyage_config(f"{fake_voyage.base_url}/v1/embeddings"))
    fake_voyage.ledger.begin_run("index-first", hold_seconds=_HOLD_SECONDS)
    client.get_embeddings_batch(["def a(): pass"])

    fake_voyage.ledger.begin_run("query-after", hold_seconds=_HOLD_SECONDS)
    with pytest.raises(Exception) as raised:
        client.get_embedding("find the slow function", embedding_purpose="query")

    assert _timeout_in_chain(raised.value), repr(raised.value)
    assert len(fake_voyage.ledger.requests("query-after")) == 1


def test_voyage_contextualized_indexing_waits_for_slow_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The temporal contextualized-embeddings request (per-commit indexing)
    waits for a response held past the configured timeout: one request,
    answered."""
    monkeypatch.setenv("VOYAGE_API_KEY", _SENTINEL_KEY)
    with _held_server(_contextualized_reply) as server:
        server.hold = _HOLD_SECONDS
        client = VoyageAIClient(
            _voyage_config(server.url, model="voyage-context-4"),
            http_client_factory=_RedirectingFactory(server.url),
        )

        result = client.get_contextualized_embeddings(
            [["commit message", "diff chunk"]],
            input_type="document",
            output_dimension=_DIMENSIONS,
        )

        assert len(result) == 1 and len(result[0]) == 2
        assert server.requests == 1, f"{server.requests} requests for one batch"


def _cohere_reply(payload: Dict[str, Any]) -> Dict[str, Any]:
    count = len(payload.get("texts") or payload.get("inputs") or [])
    return {"embeddings": {"float": [[_STUB_VALUE] * _COHERE_DIMENSIONS] * count}}


def _cohere_config(endpoint: str) -> CohereConfig:
    return CohereConfig(
        api_key=_SENTINEL_KEY,
        api_endpoint=endpoint,
        timeout=_CONFIG_TIMEOUT_SECONDS,
        max_retries=_MAX_RETRIES,
        retry_delay=0.0,
    )


def test_cohere_indexing_waits_for_slow_response() -> None:
    """A Cohere indexing batch whose response is held past the configured
    timeout is answered with one request (not abandoned and resent)."""
    with _held_server(_cohere_reply) as server:
        server.hold = _HOLD_SECONDS
        provider = CohereEmbeddingProvider(_cohere_config(server.url))

        embeddings = provider.get_embeddings_batch(["def a(): pass", "def b(): pass"])

        assert len(embeddings) == 2
        assert server.requests == 1, f"{server.requests} requests for one batch"


def test_cohere_query_embedding_keeps_configured_timeout() -> None:
    """The Cohere QUERY path is untouched: it gives up at the configured
    timeout after one attempt."""
    with _held_server(_cohere_reply) as server:
        server.hold = _HOLD_SECONDS
        provider = CohereEmbeddingProvider(_cohere_config(server.url))

        with pytest.raises(Exception) as raised:
            provider.get_embedding("find the slow function", embedding_purpose="query")

        assert _timeout_in_chain(raised.value), repr(raised.value)
        assert server.requests == 1


def test_cohere_query_timeout_not_lengthened_by_pooled_client_built_by_indexing() -> (
    None
):
    """Reverse order for Cohere: an indexing batch builds the pooled client
    first; a later query still gives up at its own configured timeout."""
    with _held_server(_cohere_reply) as server:
        server.hold = _HOLD_SECONDS
        provider = CohereEmbeddingProvider(_cohere_config(server.url))
        provider.get_embeddings_batch(["def a(): pass"])

        with pytest.raises(Exception) as raised:
            provider.get_embedding("find the slow function", embedding_purpose="query")

        assert _timeout_in_chain(raised.value), repr(raised.value)
        assert server.requests == 2


def test_cohere_multimodal_indexing_waits_for_slow_response() -> None:
    """A Cohere multimodal indexing request (document input) whose response
    is held past the configured timeout is answered with one request."""
    with _held_server(_cohere_reply) as server:
        server.hold = _HOLD_SECONDS
        client = CohereMultimodalClient(_cohere_config(server.url))

        embedding = client.get_multimodal_embedding(
            "a diagram of the indexing pipeline", [], input_type="document"
        )

        assert len(embedding) == _COHERE_DIMENSIONS
        assert server.requests == 1, f"{server.requests} requests for one batch"


def test_cohere_multimodal_query_keeps_configured_timeout() -> None:
    """The Cohere multimodal QUERY request keeps the configured timeout."""
    with _held_server(_cohere_reply) as server:
        server.hold = _HOLD_SECONDS
        config = _cohere_config(server.url)
        config.max_retries = 0
        client = CohereMultimodalClient(config)

        # The client reports the last attempt's error in its RuntimeError.
        with pytest.raises(RuntimeError, match="timed out"):
            client.get_embedding("find the pipeline diagram")

        assert server.requests == 1


def _voyage_multimodal_reply(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "data": [
            {
                "object": "embedding",
                "index": i,
                "embedding": [_STUB_VALUE] * _DIMENSIONS,
            }
            for i, _input in enumerate(payload["inputs"])
        ]
    }


def _voyage_multimodal_client(server: _HeldJsonServer) -> VoyageMultimodalClient:
    # The client hard-codes the vendor multimodal URL; the factory routes it
    # to the local stand-in through a real httpx client.
    return VoyageMultimodalClient(
        _voyage_config(server.url, model="voyage-multimodal-3"),
        http_client_factory=_RedirectingFactory(server.url),
    )


def test_voyage_multimodal_indexing_waits_for_slow_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Voyage multimodal indexing request (document input) held past the
    configured timeout is answered: one request."""
    monkeypatch.setenv("VOYAGE_API_KEY", _SENTINEL_KEY)
    with _held_server(_voyage_multimodal_reply) as server:
        server.hold = _HOLD_SECONDS
        client = _voyage_multimodal_client(server)

        embedding = client.get_multimodal_embedding(
            "a diagram of the indexing pipeline", [], input_type="document"
        )

        assert len(embedding) == _DIMENSIONS
        assert server.requests == 1


def test_voyage_multimodal_query_keeps_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Voyage multimodal QUERY request keeps the configured timeout."""
    monkeypatch.setenv("VOYAGE_API_KEY", _SENTINEL_KEY)
    with _held_server(_voyage_multimodal_reply) as server:
        server.hold = _HOLD_SECONDS
        client = _voyage_multimodal_client(server)

        with pytest.raises(Exception) as raised:
            client.get_embedding("find the pipeline diagram")

        assert _timeout_in_chain(raised.value), repr(raised.value)
        assert server.requests == 1


def test_contextual_temporal_embed_query_keeps_query_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The temporal embedder's ``embed_query`` is a QUERY: it gives up at
    the configured timeout after one attempt, never the indexing budget."""
    from types import SimpleNamespace

    from code_indexer.services.temporal.embedders.contextual import (
        ContextualTemporalEmbedder,
    )

    monkeypatch.setenv("VOYAGE_API_KEY", _SENTINEL_KEY)
    with _held_server(_contextualized_reply) as server:
        server.hold = _HOLD_SECONDS
        embedder = ContextualTemporalEmbedder(
            SimpleNamespace(voyage_ai=_voyage_config(server.url))
        )
        # The embedder's client hard-codes the vendor contextualized URL.
        embedder._client._http_client_factory = _RedirectingFactory(server.url)

        with pytest.raises(Exception) as raised:
            embedder.embed_query("when was the timeout raised")

        assert _timeout_in_chain(raised.value), repr(raised.value)
        assert server.requests == 1


def test_voyage_health_probe_is_one_attempt_at_query_budget(
    fake_voyage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-flight probe (``health_check(test_api=True)``) answers fast:
    one attempt at the configured timeout, never the indexing budget nor
    the retry loop. A response held past it reports unhealthy."""
    monkeypatch.setenv("VOYAGE_API_KEY", _SENTINEL_KEY)
    client = VoyageAIClient(_voyage_config(f"{fake_voyage.base_url}/v1/embeddings"))
    fake_voyage.ledger.begin_run("probe", hold_seconds=_HOLD_SECONDS)

    assert client.health_check(test_api=True) is False
    assert len(fake_voyage.ledger.requests("probe")) == 1


def test_cohere_health_probe_is_one_attempt_at_query_budget() -> None:
    """Cohere's pre-flight probe: one attempt at the configured timeout."""
    with _held_server(_cohere_reply) as server:
        server.hold = _HOLD_SECONDS
        provider = CohereEmbeddingProvider(_cohere_config(server.url))

        assert provider.health_check(test_api=True) is False
        assert server.requests == 1


#: The shipped default provider timeout and the indexing budget it yields.
_DEFAULT_TIMEOUT_SECONDS = 30
_DEFAULT_INDEXING_BUDGET_SECONDS = 120


@pytest.mark.parametrize("config_cls", [VoyageAIConfig, CohereConfig])
def test_default_indexing_budget_is_120_seconds(config_cls: Any) -> None:
    """Existing deployments keep ``timeout`` = 30 in their persisted config;
    indexing derives 120 s from it with no config change, queries keep 30."""
    config = config_cls()
    assert config.timeout == _DEFAULT_TIMEOUT_SECONDS

    indexing = embedding_request_timeout(
        config.timeout, config.connect_timeout, indexing=True
    )
    query = embedding_request_timeout(
        config.timeout, config.connect_timeout, indexing=False
    )

    assert (indexing.read, indexing.write, indexing.pool) == (
        _DEFAULT_INDEXING_BUDGET_SECONDS,
    ) * 3
    assert indexing.connect == config.connect_timeout
    assert (query.read, query.write, query.pool) == (_DEFAULT_TIMEOUT_SECONDS,) * 3
    assert query.connect == config.connect_timeout


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_timeout_is_refused(bad: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        embedding_request_timeout(bad, 5, indexing=True)
