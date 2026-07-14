"""
Tests for the synthetic HNSW orphan-corpus generator utility (Story #1358 AC4).

Validates that tests/utils/hnsw_orphan_corpus.py reliably reproduces both
orphan-producing regimes measured in spike #1330
(docs/research/hnsw-temporal-orphans-1330.md) against the REAL project
hnswlib fork -- no mocks, real check_integrity() calls throughout.
"""

import numpy as np
import pytest

from tests.utils.hnsw_orphan_corpus import (
    near_tie_corpus,
    exact_tie_corpus,
    build_hnsw_index,
)


class TestNearTieTemporalShaped:
    """Near-tie regime, temporal-shaped (single near-degenerate cluster)."""

    @pytest.mark.parametrize("size", [270, 1000, 5000])
    def test_produces_orphans_deterministically_single_threaded(self, size):
        vectors = near_tie_corpus(
            size=size, dim=1024, noise_scale=1e-6, pocket_fraction=1.0, seed=42
        )
        index = build_hnsw_index(vectors, num_threads=1)
        result = index.check_integrity()

        orphan_count = sum(1 for e in result["errors"] if "orphan" in e)

        assert result["valid"] is False, f"size={size} expected orphans, got none"
        assert orphan_count > 0

    def test_orphan_fraction_scales_with_size(self):
        """Spike #1330 measured near-tie orphan fraction increasing with
        corpus size (n=270 -> ~78-99, n=1000 -> 68%, n=5000 -> 85%). Exact
        thresholds are not production-calibrated (spike caveat #1); this
        test asserts the qualitative scaling behavior survives in our
        synthetic generator, not the exact spike percentages."""
        fractions = []
        for size in [270, 1000, 5000]:
            vectors = near_tie_corpus(
                size=size, dim=1024, noise_scale=1e-6, pocket_fraction=1.0, seed=42
            )
            index = build_hnsw_index(vectors, num_threads=1)
            result = index.check_integrity()
            orphan_count = sum(1 for e in result["errors"] if "orphan" in e)
            fractions.append(orphan_count / size)

        assert fractions[0] < fractions[1] < fractions[2], (
            f"Expected strictly increasing orphan fraction with size, got {fractions}"
        )

    def test_deterministic_single_threaded_build_reproducibility(self):
        """Two independent single-threaded builds from the same corpus
        must produce identical orphan sets (near-tie is a deterministic
        pruning outcome, not a race, per the spike)."""
        vectors = near_tie_corpus(
            size=1000, dim=1024, noise_scale=1e-6, pocket_fraction=1.0, seed=42
        )

        index_a = build_hnsw_index(vectors, num_threads=1)
        orphans_a = frozenset(
            int(e.split()[1])
            for e in index_a.check_integrity()["errors"]
            if "orphan" in e
        )

        index_b = build_hnsw_index(vectors, num_threads=1)
        orphans_b = frozenset(
            int(e.split()[1])
            for e in index_b.check_integrity()["errors"]
            if "orphan" in e
        )

        assert orphans_a == orphans_b


class TestNearTieRegularShaped:
    """Near-tie regime, regular-shaped (diverse majority + embedded pocket)."""

    def test_embedded_pocket_of_100_plus_orphans(self):
        """Spike measured near-tie pockets as small as ~100 elements
        orphaning inside an otherwise-diverse index."""
        vectors = near_tie_corpus(
            size=270, dim=1024, noise_scale=1e-6, pocket_fraction=0.4, seed=42
        )
        index = build_hnsw_index(vectors, num_threads=1)
        result = index.check_integrity()
        orphan_count = sum(1 for e in result["errors"] if "orphan" in e)

        assert orphan_count > 0
        assert result["valid"] is False


class TestExactTieVectorProperties:
    """Exact-tie corpus vector-level properties (bit-identical rows)."""

    def test_temporal_shaped_all_rows_bit_identical(self):
        vectors = exact_tie_corpus(size=200, dim=64, block_fraction=1.0, seed=7)
        for row in vectors[1:]:
            assert np.array_equal(row, vectors[0])

    def test_regular_shaped_block_is_bit_identical_remainder_is_diverse(self):
        vectors = exact_tie_corpus(size=1000, dim=1024, block_fraction=0.4, seed=7)
        block_n = int(1000 * 0.4)

        for row in vectors[1:block_n]:
            assert np.array_equal(row, vectors[0])

        # remainder must NOT all be identical to the block (diverse majority)
        non_identical = sum(
            1 for row in vectors[block_n:] if not np.array_equal(row, vectors[0])
        )
        assert non_identical == len(vectors) - block_n


class TestExactTieRaceRegimeSmoke:
    """Smoke check that the exact-tie regime is reproducible as a genuine
    race under multi-threaded (production) construction -- full 20-seed
    protocol lives in AC2's dedicated test module."""

    def test_at_least_one_of_five_seeds_produces_orphans_multithreaded(self):
        any_orphans = False
        for seed in range(5):
            vectors = exact_tie_corpus(
                size=1000, dim=1024, block_fraction=0.4, seed=seed
            )
            index = build_hnsw_index(vectors, num_threads=-1)
            result = index.check_integrity()
            orphan_count = sum(1 for e in result["errors"] if "orphan" in e)
            if orphan_count > 0:
                any_orphans = True
                break

        assert any_orphans, "Expected at least one of 5 seeds to race-orphan"
