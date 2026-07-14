"""
Story #1358 AC2: race-regime (exact-tie) orphan repair, fixed 20-seed protocol.

Given an index built multi-threaded from a corpus with a bit-identical block
(the race regime), across 20 seeds (spike measured 0-2 orphans/run variance
on this regime, so 20 gives ample margin to observe at least one non-zero
run without being a flaky single-shot assertion):

  - when at least one seed's build produces orphan_count > 0, repair_orphans()
    is invoked on every orphaned instance found across the 20 seeds, and
    check_integrity() reports 0 orphans for each repaired instance
  - if all 20 seeds happen to produce 0 orphans, the test is marked
    inconclusive (not a pass, not a hard fail) via pytest.skip() -- a status
    visibly distinct from PASSED/FAILED in test output/CI, never silently
    green -- and logs a note to re-run with more seeds

Real project hnswlib fork only. Zero mocks. Real check_integrity() calls.
"""

import pytest

from tests.utils.hnsw_orphan_corpus import exact_tie_corpus, build_hnsw_index

CORPUS_DIM = 1024
CORPUS_SIZE = 1000

# block_fraction == 1.0 => "temporal-shaped": the whole corpus is one
# bit-identical block (the shape in which the spike measured the race).
EXACT_TIE_BLOCK_FRACTION = 1.0

# Fixed per AC2 -- the spike measured 0-2 orphans/run variance on this
# regime; 20 seeds gives ample margin to observe at least one non-zero run
# without being a flaky single-shot assertion. Do NOT tune this down for
# speed -- it is the concrete bound resolving "flaky single-run" vs
# "unbounded seed search".
NUM_SEEDS = 20

# Production build path never calls set_num_threads -- all-core parallel,
# which is the actual trigger for this race regime.
MULTI_THREADED = -1


def test_exact_tie_race_regime_repaired_across_20_seeds():
    any_orphans_observed = False
    repaired_instance_results = []

    for seed in range(NUM_SEEDS):
        vectors = exact_tie_corpus(
            size=CORPUS_SIZE,
            dim=CORPUS_DIM,
            block_fraction=EXACT_TIE_BLOCK_FRACTION,
            seed=seed,
        )
        index = build_hnsw_index(vectors, num_threads=MULTI_THREADED)

        before = index.check_integrity()
        orphan_count_before = sum(1 for e in before["errors"] if "orphan" in e)

        if orphan_count_before == 0:
            continue

        any_orphans_observed = True
        index.repair_orphans()
        after = index.check_integrity()
        orphan_count_after = sum(1 for e in after["errors"] if "orphan" in e)

        repaired_instance_results.append(
            {
                "seed": seed,
                "orphans_before": orphan_count_before,
                "orphans_after": orphan_count_after,
                "valid": after["valid"],
            }
        )

    print(
        f"AC2 race-regime summary: {len(repaired_instance_results)}/{NUM_SEEDS} "
        f"seeds produced orphans"
    )
    for r in repaired_instance_results:
        print(
            f"  seed={r['seed']} orphans_before={r['orphans_before']} "
            f"orphans_after={r['orphans_after']} valid={r['valid']}"
        )

    if not any_orphans_observed:
        pytest.skip(
            f"INCONCLUSIVE (not pass, not fail): all {NUM_SEEDS} seeds produced "
            "0 orphans in the exact-tie race regime this run. This is a "
            "genuine race and can legitimately happen by chance. Re-run "
            "with more seeds to observe it."
        )

    for r in repaired_instance_results:
        assert r["valid"] is True, f"seed={r['seed']}: repair did not converge"
        assert r["orphans_after"] == 0, f"seed={r['seed']}: orphans remain after repair"
