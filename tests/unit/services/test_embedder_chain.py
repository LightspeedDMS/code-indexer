"""Unit tests for Story #903: Embedder Provider Chain _run_embedder_chain().

Real ProviderHealthMonitor with persistence_path=tmp_path.
Providers are real instances; only _make_sync_request is patched (HTTP boundary).
MESSI Rule 1 Anti-Mock: get_embedding() is never mocked.
"""

import os
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_VOYAGE_API_KEY = "test-voyage-key-placeholder"
_TEST_COHERE_API_KEY = "test-cohere-key-placeholder"

VOYAGE_EXPECTED_DIM: int = 1024
COHERE_EXPECTED_DIM: int = (
    1536  # embed-v4.0 default_dimension (cohere_embedding.py:137)
)
WRONG_DIM: int = 768
VOYAGE_STUB_VALUE: float = 0.1
COHERE_STUB_VALUE: float = 0.2


# ---------------------------------------------------------------------------
# Parameterized helpers
# ---------------------------------------------------------------------------


def _make_stub_vector(value: float, dim: int) -> list:
    """Build a fresh embedding vector. Parameterized to eliminate per-provider duplication."""
    return [value] * dim


def _make_embedding_response(provider: str, dim: int = VOYAGE_EXPECTED_DIM) -> dict:
    """Build a fresh API response dict for the given provider.

    provider: 'voyage' or 'cohere'. dim parameter covers normal and wrong-dim cases.
    Raises ValueError for unknown provider names to prevent silent fallback.
    """
    if provider == "voyage":
        return {"data": [{"embedding": _make_stub_vector(VOYAGE_STUB_VALUE, dim)}]}
    if provider == "cohere":
        return {"embeddings": {"float": [_make_stub_vector(COHERE_STUB_VALUE, dim)]}}
    raise ValueError(f"Unsupported provider: {provider!r}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_singleton():
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture
def health_monitor(tmp_path):
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    return ProviderHealthMonitor(persistence_path=tmp_path / "sinbin_state.json")


@pytest.fixture
def voyage_provider():
    with patch.dict(os.environ, {"VOYAGE_API_KEY": _TEST_VOYAGE_API_KEY}):
        from code_indexer.config import VoyageAIConfig
        from code_indexer.services.voyage_ai import VoyageAIClient

        yield VoyageAIClient(VoyageAIConfig(), None)


@pytest.fixture
def cohere_provider():
    with patch.dict(os.environ, {"CO_API_KEY": _TEST_COHERE_API_KEY}):
        from code_indexer.config import CohereConfig
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

        yield CohereEmbeddingProvider(CohereConfig(), None)


# ---------------------------------------------------------------------------
# Tests — Group 1: Primary success and basic fallback (3 tests)
# ---------------------------------------------------------------------------


def test_voyage_primary_success(voyage_provider, cohere_provider, health_monitor):
    """Voyage succeeds: chain returns (vector, 'voyage', None, elapsed_ms); Cohere not called."""
    from code_indexer.services.embedder_chain import _run_embedder_chain

    with (
        patch.object(
            voyage_provider,
            "_make_sync_request",
            return_value=_make_embedding_response("voyage"),
        ) as mock_voyage,
        patch.object(cohere_provider, "_make_sync_request") as mock_cohere,
    ):
        vector, name, reason, elapsed_ms, _outcomes = _run_embedder_chain(
            text="foo",
            embedding_purpose="query",
            primary_provider=voyage_provider,
            secondary_provider=cohere_provider,
            health_monitor=health_monitor,
        )

    assert vector == _make_stub_vector(VOYAGE_STUB_VALUE, VOYAGE_EXPECTED_DIM)
    assert name == "voyage"
    assert reason is None
    assert isinstance(elapsed_ms, int) and elapsed_ms >= 0
    mock_voyage.assert_called_once()
    mock_cohere.assert_not_called()


def test_voyage_failure_cohere_fallback_success(
    voyage_provider, cohere_provider, health_monitor
):
    """Voyage raises ConnectionError: chain falls over to Cohere and returns Cohere result."""
    from code_indexer.services.embedder_chain import _run_embedder_chain

    with (
        patch.object(
            voyage_provider,
            "_make_sync_request",
            side_effect=ConnectionError("Voyage unreachable"),
        ),
        patch.object(
            cohere_provider,
            "_make_sync_request",
            return_value=_make_embedding_response("cohere", dim=COHERE_EXPECTED_DIM),
        ),
    ):
        vector, name, reason, elapsed_ms, _outcomes = _run_embedder_chain(
            text="foo",
            embedding_purpose="query",
            primary_provider=voyage_provider,
            secondary_provider=cohere_provider,
            health_monitor=health_monitor,
        )

    assert vector == _make_stub_vector(COHERE_STUB_VALUE, COHERE_EXPECTED_DIM)
    assert name == "cohere"
    assert reason is None
    assert isinstance(elapsed_ms, int)


def test_both_providers_fail(voyage_provider, cohere_provider, health_monitor):
    """Both providers raise: chain returns (None, None, 'failed', elapsed_ms)."""
    from code_indexer.services.embedder_chain import _run_embedder_chain

    with (
        patch.object(
            voyage_provider,
            "_make_sync_request",
            side_effect=ConnectionError("Voyage down"),
        ),
        patch.object(
            cohere_provider,
            "_make_sync_request",
            side_effect=TimeoutError("Cohere timeout"),
        ),
    ):
        vector, name, reason, elapsed_ms, _outcomes = _run_embedder_chain(
            text="foo",
            embedding_purpose="query",
            primary_provider=voyage_provider,
            secondary_provider=cohere_provider,
            health_monitor=health_monitor,
        )

    assert vector is None
    assert name is None
    assert reason == "failed"
    assert isinstance(elapsed_ms, int)


# ---------------------------------------------------------------------------
# Tests — Group 2: Sin-bin and health-down gating (3 tests)
# Health status construction constants used in test_voyage_health_down_cohere_primary.
# ---------------------------------------------------------------------------

HEALTH_WINDOW_MINUTES: int = 60
HEALTH_SCORE_ZERO: float = 0.0
HEALTH_LATENCY_ZERO: float = 0.0
HEALTH_ERROR_RATE_FULL: float = 1.0
HEALTH_AVAILABILITY_ZERO: float = 0.0
HEALTH_TOTAL_REQUESTS: int = 10
HEALTH_SUCCESSFUL_REQUESTS: int = 0
HEALTH_FAILED_REQUESTS: int = 10


def test_voyage_sinbinned_cohere_primary(
    voyage_provider, cohere_provider, health_monitor
):
    """Voyage sin-binned: chain skips Voyage entirely and tries Cohere first."""
    from code_indexer.services.embedder_chain import (
        EMBEDDER_HEALTH_KEYS,
        _run_embedder_chain,
    )

    health_monitor.sinbin(EMBEDDER_HEALTH_KEYS["voyage"])

    with (
        patch.object(voyage_provider, "_make_sync_request") as mock_voyage,
        patch.object(
            cohere_provider,
            "_make_sync_request",
            return_value=_make_embedding_response("cohere", dim=COHERE_EXPECTED_DIM),
        ),
    ):
        vector, name, reason, elapsed_ms, _outcomes = _run_embedder_chain(
            text="foo",
            embedding_purpose="query",
            primary_provider=voyage_provider,
            secondary_provider=cohere_provider,
            health_monitor=health_monitor,
        )

    assert vector == _make_stub_vector(COHERE_STUB_VALUE, COHERE_EXPECTED_DIM)
    assert name == "cohere"
    assert reason is None
    mock_voyage.assert_not_called()


def test_voyage_health_down_cohere_primary(
    voyage_provider, cohere_provider, health_monitor
):
    """get_health() returns 'down' for Voyage: chain skips it without sinbin check."""
    from code_indexer.services.embedder_chain import (
        EMBEDDER_HEALTH_KEYS,
        _run_embedder_chain,
    )
    from code_indexer.services.provider_health_monitor import ProviderHealthStatus

    health_key = EMBEDDER_HEALTH_KEYS["voyage"]
    down_status = ProviderHealthStatus(
        provider=health_key,
        status="down",
        health_score=HEALTH_SCORE_ZERO,
        p50_latency_ms=HEALTH_LATENCY_ZERO,
        p95_latency_ms=HEALTH_LATENCY_ZERO,
        p99_latency_ms=HEALTH_LATENCY_ZERO,
        error_rate=HEALTH_ERROR_RATE_FULL,
        availability=HEALTH_AVAILABILITY_ZERO,
        total_requests=HEALTH_TOTAL_REQUESTS,
        successful_requests=HEALTH_SUCCESSFUL_REQUESTS,
        failed_requests=HEALTH_FAILED_REQUESTS,
        window_minutes=HEALTH_WINDOW_MINUTES,
    )

    def _fake_get_health(provider=None):
        if provider == health_key:
            return {health_key: down_status}
        return {}

    with patch.object(health_monitor, "get_health", side_effect=_fake_get_health):
        with (
            patch.object(voyage_provider, "_make_sync_request") as mock_voyage,
            patch.object(
                cohere_provider,
                "_make_sync_request",
                return_value=_make_embedding_response(
                    "cohere", dim=COHERE_EXPECTED_DIM
                ),
            ),
        ):
            vector, name, reason, elapsed_ms, _outcomes = _run_embedder_chain(
                text="foo",
                embedding_purpose="query",
                primary_provider=voyage_provider,
                secondary_provider=cohere_provider,
                health_monitor=health_monitor,
            )

    assert vector == _make_stub_vector(COHERE_STUB_VALUE, COHERE_EXPECTED_DIM)
    assert name == "cohere"
    assert reason is None
    mock_voyage.assert_not_called()


def test_both_providers_sinbinned(voyage_provider, cohere_provider, health_monitor):
    """Both sin-binned: chain returns (None, None, 'all-sinbinned', elapsed_ms)."""
    from code_indexer.services.embedder_chain import (
        EMBEDDER_HEALTH_KEYS,
        _run_embedder_chain,
    )

    health_monitor.sinbin(EMBEDDER_HEALTH_KEYS["voyage"])
    health_monitor.sinbin(EMBEDDER_HEALTH_KEYS["cohere"])

    with (
        patch.object(voyage_provider, "_make_sync_request") as mock_voyage,
        patch.object(cohere_provider, "_make_sync_request") as mock_cohere,
    ):
        vector, name, reason, elapsed_ms, _outcomes = _run_embedder_chain(
            text="foo",
            embedding_purpose="query",
            primary_provider=voyage_provider,
            secondary_provider=cohere_provider,
            health_monitor=health_monitor,
        )

    assert vector is None
    assert name is None
    assert reason == "all-sinbinned"
    assert isinstance(elapsed_ms, int)
    mock_voyage.assert_not_called()
    mock_cohere.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Group 3: Single-provider and no-provider scenarios (3 tests)
# ---------------------------------------------------------------------------


def test_only_voyage_configured(voyage_provider, health_monitor):
    """secondary=None: Voyage-only chain succeeds without AttributeError."""
    from code_indexer.services.embedder_chain import _run_embedder_chain

    with patch.object(
        voyage_provider,
        "_make_sync_request",
        return_value=_make_embedding_response("voyage"),
    ):
        vector, name, reason, elapsed_ms, _outcomes = _run_embedder_chain(
            text="foo",
            embedding_purpose="query",
            primary_provider=voyage_provider,
            secondary_provider=None,
            health_monitor=health_monitor,
        )

    assert vector == _make_stub_vector(VOYAGE_STUB_VALUE, VOYAGE_EXPECTED_DIM)
    assert name == "voyage"
    assert reason is None


def test_only_cohere_configured(cohere_provider, health_monitor):
    """primary=None: Cohere-only chain succeeds without AttributeError."""
    from code_indexer.services.embedder_chain import _run_embedder_chain

    with patch.object(
        cohere_provider,
        "_make_sync_request",
        return_value=_make_embedding_response("cohere", dim=COHERE_EXPECTED_DIM),
    ):
        vector, name, reason, elapsed_ms, _outcomes = _run_embedder_chain(
            text="foo",
            embedding_purpose="query",
            primary_provider=None,
            secondary_provider=cohere_provider,
            health_monitor=health_monitor,
        )

    assert vector == _make_stub_vector(COHERE_STUB_VALUE, COHERE_EXPECTED_DIM)
    assert name == "cohere"
    assert reason is None


def test_no_providers_configured(health_monitor):
    """primary=None and secondary=None: chain returns 'no-providers-configured' without raising."""
    from code_indexer.services.embedder_chain import _run_embedder_chain

    vector, name, reason, elapsed_ms, _outcomes = _run_embedder_chain(
        text="foo",
        embedding_purpose="query",
        primary_provider=None,
        secondary_provider=None,
        health_monitor=health_monitor,
    )

    assert vector is None
    assert name is None
    assert reason == "no-providers-configured"
    assert isinstance(elapsed_ms, int)


# ---------------------------------------------------------------------------
# Tests — Group 4: Health recording, elapsed_ms, and RuntimeError handling (3 tests)
# ---------------------------------------------------------------------------

ELAPSED_MS_TOLERANCE: int = 10  # ms overhead allowance for monotonic clock measurement


def test_provider_records_health_via_monitor(voyage_provider, health_monitor):
    """Successful Voyage call is recorded as success=True via health_monitor.record_call."""
    from code_indexer.services.embedder_chain import (
        EMBEDDER_HEALTH_KEYS,
        _run_embedder_chain,
    )

    health_key = EMBEDDER_HEALTH_KEYS["voyage"]

    with patch.object(
        voyage_provider,
        "_make_sync_request",
        return_value=_make_embedding_response("voyage"),
    ):
        _run_embedder_chain(
            text="foo",
            embedding_purpose="query",
            primary_provider=voyage_provider,
            secondary_provider=None,
            health_monitor=health_monitor,
        )

    health = health_monitor.get_health(health_key)
    status = health.get(health_key)
    assert status is not None
    assert status.successful_requests >= 1
    assert status.failed_requests == 0


def test_elapsed_ms_always_returned_even_on_total_failure(health_monitor):
    """elapsed_ms is a non-negative int even on the no-providers path."""
    import time

    from code_indexer.services.embedder_chain import _run_embedder_chain

    t_before = time.monotonic()
    _, _, _, elapsed_ms, _outcomes = _run_embedder_chain(
        text="foo",
        embedding_purpose="query",
        primary_provider=None,
        secondary_provider=None,
        health_monitor=health_monitor,
    )
    t_after = time.monotonic()

    assert isinstance(elapsed_ms, int)
    assert elapsed_ms >= 0
    assert elapsed_ms <= int((t_after - t_before) * 1000) + ELAPSED_MS_TOLERANCE


def test_providers_attempted_carries_per_provider_reasons(
    voyage_provider, cohere_provider, health_monitor
):
    """EmbedderUnavailableError.providers_attempted carries distinct per-provider reasons (BLOCKER 2).

    When voyage-embedder is sin-binned and cohere fails, the chain exposes a 5th
    return element `providers_outcomes: List[Tuple[str, str]]` so callers can
    populate EmbedderUnavailableError.providers_attempted with accurate per-provider
    information rather than the same aggregate reason for both providers.

    This test drives the full contract: chain returns distinct per-provider reasons,
    which the caller uses to construct EmbedderUnavailableError, and the error's
    providers_attempted field reflects those distinct reasons.
    """
    from code_indexer.services.embedder_chain import (
        EMBEDDER_HEALTH_KEYS,
        EmbedderUnavailableError,
        _run_embedder_chain,
    )

    health_monitor.sinbin(EMBEDDER_HEALTH_KEYS["voyage"])

    with patch.object(
        cohere_provider,
        "_make_sync_request",
        side_effect=ConnectionError("Cohere also down"),
    ):
        vector, name, reason, elapsed_ms, providers_outcomes = _run_embedder_chain(
            text="foo",
            embedding_purpose="query",
            primary_provider=voyage_provider,
            secondary_provider=cohere_provider,
            health_monitor=health_monitor,
        )

    assert vector is None
    assert name is None

    # providers_outcomes is a list of (provider_name, per_provider_reason) tuples
    assert isinstance(providers_outcomes, list), (
        f"providers_outcomes must be a list, got {type(providers_outcomes)}"
    )
    outcome_map = dict(providers_outcomes)

    # Voyage was sinbinned — its reason must indicate that, not "failed"
    assert outcome_map.get("voyage") == "sinbinned", (
        f"Expected 'sinbinned' for voyage, got {outcome_map.get('voyage')!r}"
    )
    # Cohere actually attempted and raised — its reason must be "failed"
    assert outcome_map.get("cohere") == "failed", (
        f"Expected 'failed' for cohere, got {outcome_map.get('cohere')!r}"
    )

    # Build the error from providers_outcomes (as cli.py will do after the fix)
    exc = EmbedderUnavailableError(
        f"Both embedding providers unavailable: {reason}",
        providers_attempted=providers_outcomes,
    )
    attempted_map = dict(exc.providers_attempted)
    assert attempted_map.get("voyage") == "sinbinned", (
        f"EmbedderUnavailableError.providers_attempted must have 'sinbinned' for voyage, "
        f"got {attempted_map.get('voyage')!r}"
    )
    assert attempted_map.get("cohere") == "failed", (
        f"EmbedderUnavailableError.providers_attempted must have 'failed' for cohere, "
        f"got {attempted_map.get('cohere')!r}"
    )


def test_failover_emits_info_log_when_secondary_succeeds(
    voyage_provider, cohere_provider, health_monitor, caplog
):
    """Exactly one INFO log containing 'Embedder failover' is emitted when
    Voyage fails and Cohere succeeds (SHOULD-FIX 4 / Story #904 AC requirement).
    """
    import logging

    from code_indexer.services.embedder_chain import _run_embedder_chain

    with (
        patch.object(
            voyage_provider,
            "_make_sync_request",
            side_effect=ConnectionError("Voyage unreachable"),
        ),
        patch.object(
            cohere_provider,
            "_make_sync_request",
            return_value=_make_embedding_response("cohere", dim=COHERE_EXPECTED_DIM),
        ),
        caplog.at_level(logging.INFO, logger="code_indexer.services.embedder_chain"),
    ):
        vector, name, reason, elapsed_ms, _outcomes = _run_embedder_chain(
            text="foo",
            embedding_purpose="query",
            primary_provider=voyage_provider,
            secondary_provider=cohere_provider,
            health_monitor=health_monitor,
        )

    assert vector is not None
    assert name == "cohere"

    failover_logs = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "Embedder failover" in r.getMessage()
    ]
    assert len(failover_logs) == 1, (
        f"Expected exactly one INFO 'Embedder failover' log, got: {[r.getMessage() for r in caplog.records]}"
    )
    msg = failover_logs[0].getMessage()
    assert "voyage" in msg.lower(), f"Failover log must mention 'voyage': {msg}"
    assert "cohere" in msg.lower(), f"Failover log must mention 'cohere': {msg}"


def test_voyage_returns_wrong_dimension_embedding(
    voyage_provider, cohere_provider, health_monitor
):
    """RuntimeError from _validate_embeddings is caught as 'failed'; chain falls to Cohere.

    Asserts both that Cohere fallback result is returned AND that Voyage failure was
    recorded in the health monitor (failed_requests >= 1), proving the RuntimeError
    triggered the failure branch rather than propagating to the caller.
    """
    from code_indexer.services.embedder_chain import (
        EMBEDDER_HEALTH_KEYS,
        _run_embedder_chain,
    )

    voyage_health_key = EMBEDDER_HEALTH_KEYS["voyage"]

    with (
        patch.object(
            voyage_provider,
            "_make_sync_request",
            return_value=_make_embedding_response("voyage", dim=WRONG_DIM),
        ),
        patch.object(
            cohere_provider,
            "_make_sync_request",
            return_value=_make_embedding_response("cohere", dim=COHERE_EXPECTED_DIM),
        ),
    ):
        vector, name, reason, elapsed_ms, _outcomes = _run_embedder_chain(
            text="foo",
            embedding_purpose="query",
            primary_provider=voyage_provider,
            secondary_provider=cohere_provider,
            health_monitor=health_monitor,
        )

    # Fallback succeeds — Cohere result returned.
    assert vector == _make_stub_vector(COHERE_STUB_VALUE, COHERE_EXPECTED_DIM)
    assert name == "cohere"
    assert reason is None

    # Voyage failure must be recorded — proves RuntimeError was caught as 'failed'.
    health = health_monitor.get_health(voyage_health_key)
    voyage_status = health.get(voyage_health_key)
    assert voyage_status is not None
    assert voyage_status.failed_requests >= 1
