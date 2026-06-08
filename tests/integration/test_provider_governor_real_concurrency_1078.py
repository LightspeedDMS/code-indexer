"""C4 — Real-provider concurrency regression test for Bug #1078.

Proves that ProviderConcurrencyGovernor correctly bounds concurrent REAL
VoyageAI HTTP calls to K=8 (default query_provider_max_concurrency) and
that the system does not hang under a 20-thread simultaneous burst.

The test exercises the EXACT production code path used by all 5 serving
sites: governed_query_embedding() from server/services/governed_call.py,
which wraps execute_with_backoff(governor.execute(..., lambda: provider.get_embedding(...))).

Skip conditions:
  - VOYAGE_API_KEY not set (neither VOYAGE_API_KEY nor E2E_VOYAGE_API_KEY)

Run manually:
    PYTHONPATH=./src python3 -m pytest tests/integration/test_provider_governor_real_concurrency_1078.py -p no:cacheprovider -v -s
"""

import os
import threading
import time
from typing import TYPE_CHECKING, List, Optional, Union

if TYPE_CHECKING:
    from code_indexer.services.voyage_ai import VoyageAIClient

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_voyage_api_key() -> Optional[str]:
    """Return VOYAGE_API_KEY or E2E_VOYAGE_API_KEY, whichever is set."""
    return os.environ.get("VOYAGE_API_KEY") or os.environ.get("E2E_VOYAGE_API_KEY")


# Module-level skip when the key is absent — clean, no stderr noise.
_VOYAGE_KEY = _resolve_voyage_api_key()
pytestmark = pytest.mark.skipif(
    not _VOYAGE_KEY,
    reason="VOYAGE_API_KEY / E2E_VOYAGE_API_KEY not set — skipping real-provider tests",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONCURRENCY = 20  # threads launched simultaneously
EXPECTED_K = 8  # default query_provider_max_concurrency
EXPECTED_MIN_WAITS = CONCURRENCY - EXPECTED_K  # at least 12 acquisitions had to wait
BURST_DEADLINE_SECS = 120.0  # wall-clock budget for all 20 calls to finish
SENTINEL_CALL_DEADLINE_SECS = 2.0  # governor must self-recover after burst


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_governor():
    """Destroy and recreate the governor singleton before each test."""
    from code_indexer.server.services.provider_concurrency_governor import (
        ProviderConcurrencyGovernor,
    )

    ProviderConcurrencyGovernor.reset_instance()
    yield
    # Leave singleton intact for next test; reset guarantees isolation
    ProviderConcurrencyGovernor.reset_instance()


@pytest.fixture()
def voyage_provider():
    """Return a real VoyageAIClient using VOYAGE_API_KEY from the environment."""
    from code_indexer.config import VoyageAIConfig
    from code_indexer.services.voyage_ai import VoyageAIClient

    return VoyageAIClient(VoyageAIConfig())


# ---------------------------------------------------------------------------
# Test C4-A — Embedding burst: K=8 cap held, ≥12 waited, no hang
# ---------------------------------------------------------------------------


def _run_one_governed_call(
    provider: object,
    text: str,
    result_slot: List[Optional[Union[List[float], Exception]]],
    idx: int,
    barrier: threading.Barrier,
) -> None:
    """Thread target: wait at barrier, fire one governed embedding, store result."""
    barrier.wait()  # synchronise with all other threads
    try:
        from code_indexer.server.services.governed_call import (
            governed_query_embedding,
        )

        embedding = governed_query_embedding(provider, text)
        result_slot[idx] = embedding
    except Exception as exc:
        result_slot[idx] = exc


class TestProviderGovernorRealConcurrency:
    """C4 — real Voyage API key, real HTTP, no mocks."""

    def test_embedding_burst_bounds_concurrency(
        self, voyage_provider: "VoyageAIClient"
    ) -> None:
        """20 simultaneous governed embedding calls — K=8 cap held, ≥12 waited."""
        from code_indexer.server.services.provider_concurrency_governor import (
            ProviderConcurrencyGovernor,
        )

        governor = ProviderConcurrencyGovernor.get_instance()
        assert governor._k == EXPECTED_K, (
            f"Expected default K={EXPECTED_K}, got {governor._k}. "
            "Reset the singleton or adjust EXPECTED_K."
        )

        # Pre-burst acquire count snapshot (should be zero after fixture reset)
        pre_burst_total_acquires = sum(governor.acquire_wait_count.values())

        barrier = threading.Barrier(CONCURRENCY)
        results: List[Optional[Union[List[float], Exception]]] = [None] * CONCURRENCY
        threads = [
            threading.Thread(
                target=_run_one_governed_call,
                args=(
                    voyage_provider,
                    f"concurrency test query {i}",
                    results,
                    i,
                    barrier,
                ),
                daemon=True,
            )
            for i in range(CONCURRENCY)
        ]

        t_start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=BURST_DEADLINE_SECS)

        elapsed = time.monotonic() - t_start

        # --- All threads must have completed within the deadline ---
        alive = [t for t in threads if t.is_alive()]
        assert not alive, (
            f"{len(alive)} of {CONCURRENCY} threads still alive after "
            f"{BURST_DEADLINE_SECS}s — governor may be hanging"
        )

        print(f"\n[C4] Burst elapsed: {elapsed:.2f}s")

        # --- All calls must have returned a result (embedding or clean error) ---
        none_slots = [i for i, r in enumerate(results) if r is None]
        assert not none_slots, (
            f"Slots {none_slots} have no result — threads did not store"
        )

        # Count successes and errors
        successes = [r for r in results if isinstance(r, list)]
        errors = [r for r in results if isinstance(r, Exception)]
        print(f"[C4] Successes: {len(successes)}, Errors: {len(errors)}")

        # All successes must be non-empty float vectors
        for i, emb in enumerate(successes):
            assert len(emb) > 0, f"Result {i} is an empty embedding list"
            assert isinstance(emb[0], float), f"Result {i} element[0] is not float"

        # --- Governor telemetry assertions ---
        hwm = governor.in_flight_high_water_mark["voyage"]
        waits = governor.acquire_wait_count["voyage"] - pre_burst_total_acquires

        print(f"[C4] in_flight_high_water_mark[voyage] = {hwm}")
        print(f"[C4] acquire_wait_count[voyage] = {waits}")

        assert hwm == EXPECTED_K, (
            f"High-water mark {hwm} != {EXPECTED_K}: "
            "governor did not cap concurrent calls at K"
        )
        assert waits >= EXPECTED_MIN_WAITS, (
            f"wait_count {waits} < {EXPECTED_MIN_WAITS}: "
            "fewer threads waited than expected for K={EXPECTED_K} cap"
        )

    def test_governor_self_recovery_after_burst(
        self, voyage_provider: "VoyageAIClient"
    ) -> None:
        """Sentinel call after burst completes quickly — no residual slot starvation."""
        from code_indexer.server.services.governed_call import governed_query_embedding

        t_start = time.monotonic()
        result = governed_query_embedding(voyage_provider, "sentinel recovery query")
        elapsed = time.monotonic() - t_start

        print(f"\n[C4] Sentinel call elapsed: {elapsed:.3f}s")

        assert isinstance(result, list) and len(result) > 0, (
            "Sentinel governed call returned an empty/invalid embedding"
        )
        assert elapsed < SENTINEL_CALL_DEADLINE_SECS, (
            f"Sentinel call took {elapsed:.3f}s >= {SENTINEL_CALL_DEADLINE_SECS}s — "
            "governor did not recover quickly after burst"
        )

    def test_indexing_path_bypasses_governor(
        self, voyage_provider: "VoyageAIClient"
    ) -> None:
        """get_embeddings_batch (indexing path) must NOT touch the governor."""
        from code_indexer.server.services.provider_concurrency_governor import (
            ProviderConcurrencyGovernor,
        )

        governor = ProviderConcurrencyGovernor.get_instance()
        pre_wait_count = dict(governor.acquire_wait_count)
        pre_hwm = dict(governor.in_flight_high_water_mark)

        # Indexing path: batch embedding with retry=True (default)
        result = voyage_provider.get_embeddings_batch(
            ["indexing path test query"], retry=True
        )

        post_wait_count = dict(governor.acquire_wait_count)
        post_hwm = dict(governor.in_flight_high_water_mark)

        assert result and len(result) == 1 and len(result[0]) > 0, (
            "Indexing batch embedding returned empty result"
        )

        # Governor counters must be unchanged — indexing never acquires a slot
        assert post_wait_count == pre_wait_count, (
            f"acquire_wait_count changed during indexing call: "
            f"{pre_wait_count} -> {post_wait_count}"
        )
        assert post_hwm == pre_hwm, (
            f"in_flight_high_water_mark changed during indexing call: "
            f"{pre_hwm} -> {post_hwm}"
        )

        print("\n[C4] Indexing path did not touch governor (confirmed)")
