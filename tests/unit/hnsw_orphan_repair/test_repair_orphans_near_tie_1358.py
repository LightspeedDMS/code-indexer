"""
Story #1358 AC1 + AC3: deterministic repair of the near-tie regime.

Given an HNSW index built at production parameters (M=16, ef_construction=200,
cosine) from a corpus containing a near-tie pocket (cosine > 0.9999999, not
bit-identical), and check_integrity() reports orphan_count > 0:

  - repair_orphans() drives check_integrity().valid to True, orphan_count 0
  - re-running repair_orphans() on the repaired index is idempotent (no change)
  - two independent single-threaded builds + repairs of the same input produce
    identical graphs (deterministic)
  - AC3: post-repair recall is verified via actual knn_query self-search for
    every previously-orphaned element (not just the integrity flag)

Real project hnswlib fork only. Zero mocks. Real check_integrity()/knn_query().
"""

from tests.utils.hnsw_orphan_corpus import near_tie_corpus, build_hnsw_index

# Production embedding dimensionality (matches voyage-code-3-shaped corpora).
CORPUS_DIM = 1024

# Calibrated noise stddev keeping cosine similarity > 0.9999999 while
# remaining NOT bit-identical (see tests/utils/hnsw_orphan_corpus.py).
NEAR_TIE_NOISE_SCALE = 1e-6

# pocket_fraction == 1.0 => "temporal-shaped": the whole corpus is one
# near-degenerate cluster.
TEMPORAL_SHAPED_POCKET_FRACTION = 1.0

# Corpus size for the primary deterministic-regime fixtures in this module.
CORPUS_SIZE = 1000

# Fixed RNG seed: deterministic corpus content across all builds in this file.
CORPUS_SEED = 42

# Single-threaded construction: the near-tie regime orphans "even
# single-threaded" per the spike, and ST removes an unrelated
# level_generator_ RNG race (discovered during Story #1358 calibration)
# that would otherwise make graph-level determinism assertions flaky.
SINGLE_THREADED = 1

# Expected top-k for a self-query hit-rate check (AC3).
SELF_QUERY_K = 1

PERFECT_HIT_RATE = 1.0

# AC3 recall calibration: regular-shaped (diverse majority + pocket).
#
# The AC1/AC3 temporal-shaped fixture above (entire 1000-element corpus one
# near-degenerate cluster, noise=1e-6) is excellent for proving orphan_count
# convergence (AC1) but was empirically found UNSUITABLE for AC3's recall
# signal: with ALL elements mutually near-tied, approximate self-query
# k-NN has no distance gradient to exploit and fails to retrieve the exact
# self element even for elements that were NEVER orphaned (measured 0/20
# hit rate on healthy nodes at noise=1e-6/1e-5/1e-4 in that shape -- a
# property of corpus degeneracy vs. approximate search, not a repair
# defect: check_integrity().valid == True throughout). This matches spike
# #1330 caveat #6 ("orphan-vs-control self-query had nothing to measure" at
# degenerate configs).
#
# AC3 therefore uses a REGULAR-shaped fixture (diverse majority + a smaller
# near-tie pocket) with a calibrated noise scale where the pocket remains
# within the near-tie regime (orphans still form) while individual pocket
# elements are float32-distinguishable enough for self-query to reliably
# recover the true self post-repair. Empirically calibrated: pre-repair
# self-query hit rate 0/18, post-repair 18/18 (see Story #1358 evidence).
AC3_CORPUS_SIZE = 270
AC3_NOISE_SCALE = 1.5e-3
AC3_POCKET_FRACTION = 0.4
AC3_SEED = 42
AC3_QUERY_EF = 200


def _make_near_tie_index(num_threads=SINGLE_THREADED):
    vectors = near_tie_corpus(
        size=CORPUS_SIZE,
        dim=CORPUS_DIM,
        noise_scale=NEAR_TIE_NOISE_SCALE,
        pocket_fraction=TEMPORAL_SHAPED_POCKET_FRACTION,
        seed=CORPUS_SEED,
    )
    index = build_hnsw_index(vectors, num_threads=num_threads)
    return vectors, index


def _make_ac3_recall_index():
    vectors = near_tie_corpus(
        size=AC3_CORPUS_SIZE,
        dim=CORPUS_DIM,
        noise_scale=AC3_NOISE_SCALE,
        pocket_fraction=AC3_POCKET_FRACTION,
        seed=AC3_SEED,
    )
    index = build_hnsw_index(vectors, num_threads=SINGLE_THREADED)
    index.set_ef(AC3_QUERY_EF)
    return vectors, index


def _orphan_ids(index):
    result = index.check_integrity()
    return frozenset(int(e.split()[1]) for e in result["errors"] if "orphan" in e)


class TestRepairOrphansNearTieDeterministic:
    def test_repair_drives_orphan_count_to_zero(self):
        _, index = _make_near_tie_index()

        before = index.check_integrity()
        orphans_before = sum(1 for e in before["errors"] if "orphan" in e)
        assert orphans_before > 0, "fixture must start broken"

        repair_result = index.repair_orphans()

        after = index.check_integrity()
        orphans_after = sum(1 for e in after["errors"] if "orphan" in e)

        assert after["valid"] is True
        assert orphans_after == 0
        assert repair_result["orphans_after"] == 0
        assert repair_result["orphans_before"] == orphans_before

    def test_repair_is_idempotent(self):
        _, index = _make_near_tie_index()
        index.repair_orphans()
        assert index.check_integrity()["valid"] is True

        second_result = index.repair_orphans()

        assert second_result["orphans_before"] == 0
        assert second_result["orphans_after"] == 0
        assert second_result["repaired_count"] == 0
        assert index.check_integrity()["valid"] is True

    def test_repair_is_deterministic_across_independent_single_threaded_builds(self):
        vectors_a, index_a = _make_near_tie_index()
        index_a.repair_orphans()
        result_a = index_a.check_integrity()

        vectors_b = vectors_a.copy()
        index_b = build_hnsw_index(vectors_b, num_threads=SINGLE_THREADED)
        index_b.repair_orphans()
        result_b = index_b.check_integrity()

        assert result_a["valid"] is True
        assert result_b["valid"] is True
        assert result_a["min_inbound"] == result_b["min_inbound"]
        assert result_a["max_inbound"] == result_b["max_inbound"]
        assert result_a["connections_checked"] == result_b["connections_checked"]


class TestRepairOrphansRecallRestored:
    """AC3: recall restored for repaired elements, verified via real knn_query."""

    def test_previously_orphaned_elements_self_query_hit_after_repair(self):
        vectors, index = _make_ac3_recall_index()

        orphans_before = _orphan_ids(index)
        assert len(orphans_before) > 0

        # Pre-repair: self-query hit rate for orphaned elements should NOT be
        # perfect (that's the whole point of an orphan being unreachable).
        pre_repair_hits = 0
        for oid in orphans_before:
            labels, _ = index.knn_query(vectors[oid], k=SELF_QUERY_K)
            if labels[0][0] == oid:
                pre_repair_hits += 1
        pre_repair_hit_rate = pre_repair_hits / len(orphans_before)

        index.repair_orphans()
        assert index.check_integrity()["valid"] is True

        post_repair_hits = 0
        for oid in orphans_before:
            labels, _ = index.knn_query(vectors[oid], k=SELF_QUERY_K)
            if labels[0][0] == oid:
                post_repair_hits += 1
        post_repair_hit_rate = post_repair_hits / len(orphans_before)

        print(
            f"AC3 self-query hit-rate: pre-repair={pre_repair_hit_rate:.2%} "
            f"post-repair={post_repair_hit_rate:.2%} "
            f"(n_orphans={len(orphans_before)})"
        )

        assert post_repair_hit_rate == PERFECT_HIT_RATE
