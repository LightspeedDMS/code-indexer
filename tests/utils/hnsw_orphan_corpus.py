"""
Synthetic HNSW orphan-corpus generator (Story #1358 / Epic #1333, AC4).

Reproduces the two orphan-producing regimes measured in spike #1330
(docs/research/hnsw-temporal-orphans-1330.md) against the REAL project
hnswlib fork, at production build parameters (M=16, ef_construction=200,
cosine, batched add_items -- no mocks, no PyPI hnswlib).

Two measured regimes:

  - NEAR-TIE (deterministic): vectors extremely close (cosine > 0.9999999)
    but NOT bit-identical. getNeighborsByHeuristic2's M-bounded pruning
    genuinely discards nodes from all their chosen neighbors' lists.
    The spike measured this regime as orphaning "even single-threaded"
    (ST == MT qualitatively). During Story #1358 calibration we ALSO
    discovered that hnswlib's `level_generator_` (a single shared,
    unsynchronized `std::default_random_engine`) makes ANY multi-threaded
    `add_items` build non-deterministic in raw graph structure --
    independent of, and in addition to, the near-tie orphaning mechanism
    itself. Consequently, tests that assert graph-level determinism for
    the near-tie regime MUST build with `num_threads=1`; this is still a
    faithful reproduction of the regime (orphan counts are close in
    magnitude between ST and MT), it simply removes an unrelated
    known-nondeterministic build-time RNG race so the DETERMINISM
    assertion is provable rather than flaky.

  - EXACT-TIE (race): bit-identical vectors (cosine == 1.0). A
    multi-threaded `add_items` data race on neighbor back-links leaves
    some tied nodes with zero inbound edges. NON-deterministic run to
    run; requires `num_threads=-1` (all-core, matching the production
    build path which never calls `set_num_threads`) to manifest.

Two construction shapes, matching the spike's own terminology:

  - "temporal-shaped": the corpus is a SINGLE globally near-degenerate
    cluster (`pocket_fraction` / `block_fraction` == 1.0) -- mirrors a
    per-commit temporal shard of near-identical embeddings.
  - "regular-shaped": a diverse random majority with an embedded near-tie
    or exact-tie pocket (`pocket_fraction` / `block_fraction` < 1.0) --
    mirrors a regular semantic index containing a vendored/minified/
    boilerplate block.

Exact orphan-rate thresholds are NOT production-calibrated (spike caveat
#1); this generator preserves the qualitative behavior measured in the
spike (near-tie orphan fraction increases with corpus size; near-tie
pockets as small as ~100 elements can orphan inside a diverse index;
exact-tie blocks race non-deterministically under multi-threaded builds)
rather than reproducing exact spike percentages.
"""

from typing import Any

import numpy as np
import hnswlib


def near_tie_corpus(
    size: int,
    dim: int = 1024,
    noise_scale: float = 1e-6,
    pocket_fraction: float = 1.0,
    seed: int = 42,
) -> np.ndarray:
    """Generate a corpus reproducing the NEAR-TIE deterministic regime.

    Args:
        size: total number of vectors in the corpus.
        dim: embedding dimensionality (production default 1024).
        noise_scale: per-dimension gaussian noise stddev added to the near-tie
            base vector. 1e-6 (calibrated) keeps cosine similarity > 0.9999999
            while remaining NOT bit-identical.
        pocket_fraction: fraction of `size` that is the near-tie pocket.
            1.0 => "temporal-shaped" (the entire corpus is one near-degenerate
            cluster). < 1.0 => "regular-shaped" (a diverse random majority
            with the first `int(size * pocket_fraction)` rows overwritten by
            the near-tie pocket, embedded among diverse content).
        seed: RNG seed -- fully deterministic given the same seed.

    Returns:
        (size, dim) float32 array.
    """
    rng = np.random.RandomState(seed)

    if pocket_fraction >= 1.0:
        base = rng.randn(dim).astype(np.float32)
        noise = rng.normal(0, noise_scale, size=(size, dim)).astype(np.float32)
        return np.tile(base, (size, 1)) + noise

    vectors = rng.randn(size, dim).astype(np.float32)
    pocket_n = int(size * pocket_fraction)
    base = rng.randn(dim).astype(np.float32)
    noise = rng.normal(0, noise_scale, size=(pocket_n, dim)).astype(np.float32)
    vectors[:pocket_n] = np.tile(base, (pocket_n, 1)) + noise
    return vectors


def exact_tie_corpus(
    size: int,
    dim: int = 1024,
    block_fraction: float = 1.0,
    seed: int = 42,
) -> np.ndarray:
    """Generate a corpus reproducing the EXACT-TIE race regime.

    Args:
        size: total number of vectors in the corpus.
        dim: embedding dimensionality (production default 1024).
        block_fraction: fraction of `size` that is bit-identical (cosine ==
            1.0 exactly). 1.0 => "temporal-shaped" (whole corpus is one
            bit-identical block). < 1.0 => "regular-shaped" (a diverse
            random majority with the first `int(size * block_fraction)` rows
            overwritten by an exact-duplicate block).
        seed: RNG seed -- deterministic vector CONTENT (the resulting build
            is still subject to the genuine multi-threaded race; that
            non-determinism is a property of the build, not this generator).

    Returns:
        (size, dim) float32 array.
    """
    rng = np.random.RandomState(seed)

    if block_fraction >= 1.0:
        base = rng.randn(dim).astype(np.float32)
        return np.tile(base, (size, 1)).astype(np.float32)

    vectors = rng.randn(size, dim).astype(np.float32)
    block_n = int(size * block_fraction)
    base = rng.randn(dim).astype(np.float32)
    vectors[:block_n] = np.tile(base, (block_n, 1))
    return vectors


def build_hnsw_index(
    vectors: np.ndarray,
    space: str = "cosine",
    M: int = 16,
    ef_construction: int = 200,
    random_seed: int = 100,
    num_threads: int = -1,
) -> Any:
    """Build a production-parameter HNSW index from a corpus.

    Matches the shared production build path (HNSWIndexManager /
    build_hnsw_index_to_temp): M=16, ef_construction=200, cosine, ONE
    batched add_items call, labels = np.arange(len(vectors)). Production
    code never calls set_num_threads, so num_threads=-1 (all-core,
    hnswlib's default) is the production-faithful setting; pass
    num_threads=1 explicitly to get a reproducible single-threaded build
    (needed to test the near-tie regime's determinism -- see module
    docstring on the level_generator_ race).

    Returns:
        A real hnswlib.Index (no mocks) with the corpus already inserted.
    """
    dim = vectors.shape[1]
    index = hnswlib.Index(space=space, dim=dim)
    index.init_index(
        max_elements=len(vectors),
        M=M,
        ef_construction=ef_construction,
        random_seed=random_seed,
    )
    labels = np.arange(len(vectors))
    # Copy defensively: add_items under num_threads=-1 processes the buffer
    # concurrently; callers must not observe/rely on aliasing with `vectors`.
    index.add_items(vectors.copy(), labels, num_threads=num_threads)
    return index
