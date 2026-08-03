"""Story #1493 code-review follow-up: does AC1's combined-overfetch-ceiling
cap ever cause the LEGACY SHARDED_JSON temporal path to drop a genuinely
relevant result?

AC2's decode-avoidance mechanism (is_head_chunk_id short-circuit) is
CHUNKS_DB-only -- it does not touch the legacy SHARDED_JSON Case B
hydration loop at all. So any recall risk introduced by AC1's cap lands
specifically on SHARDED_JSON collections (the layout a plain
`cidx index --index-commits` run actually produces today). This module
builds a DETERMINISTIC, ENGINEERED-vector scenario -- run through the REAL
FilesystemVectorStore.search() + real HNSW + real legacy vector_*.json
hydration + the REAL TemporalSearchService._filter_by_time_range()
post-filter (no mocking of the code under test) -- that places a genuinely
relevant head-chunk's raw HNSW similarity rank EXACTLY in the window AC1's
cap would exclude for the report's documented worst case (true_user_limit=3,
chunk_type=commit_message: natural search_limit=3*3*40=360,
TEMPORAL_COMBINED_OVERFETCH_CEILING=60 -> capped search_limit=3*60=180).

Vectors are constructed analytically (cos/sin combination of a fixed query
vector and a fixed orthogonal vector) so each candidate's cosine similarity
to the query -- and therefore its exact HNSW rank -- is fully deterministic
and reproducible, unlike a real-embedding-API-based test whose exact
numeric outcome depends on live embedding-model geometry. `ef` (HNSW's
dynamic candidate-list size) is set well above the total corpus size so
the graph is explored exhaustively at this small scale -- eliminating
HNSW's normal approximate-search variance for this test's purposes.

Result (see test bodies): with search_limit=360 (pre-#1493 natural, or any
value >= the target's true rank), the target is correctly returned as one
of the top-3 relevant results. With search_limit=180 (AC1's capped value),
the target's rank (182) falls OUTSIDE the capped candidate window, so it is
NOT returned -- FEWER than the requested 3 results come back. This is a
REAL, reproducible instance of the theoretical recall risk the code review
asked to see demonstrated, not merely asserted away. See
reports/perf/temporal_overfetch_1493_*.json for the accompanying real-world
(real embeddings) evidence showing this specific failure mode did not
manifest across 7 real, natural-language queries -- the risk is real in the
adversarial case constructed here, but requires an adversarial rank
distribution (a genuine match ranked far behind a page's worth of unrelated
near-duplicate candidates with NO other genuine matches in between) that
natural-language semantic queries do not appear to produce in practice,
because real embeddings cluster topically-similar text (including a
message chunk against a natural-language query) more tightly than they
cluster against unrelated but lexically-repetitive filler.
"""

from pathlib import Path
from typing import List, Tuple, cast

import numpy as np
import pytest

from code_indexer.services.temporal.temporal_point_builder import build_point_id
from code_indexer.services.temporal.temporal_search_service import (
    ALL_TIME_RANGE,
    TemporalSearchResult,
    TemporalSearchService,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 64
RNG_SEED = 99

# Mirrors the report's documented worst case exactly: true_user_limit=3,
# chunk_type=commit_message -> shard_multiplier(3) * chunk_type_multiplier(40)
# = natural 360; TEMPORAL_COMBINED_OVERFETCH_CEILING(60) * 3 = capped 180.
NATURAL_SEARCH_LIMIT = 360
CAPPED_SEARCH_LIMIT = 180
DISPLAY_LIMIT = 3

# ef must exceed the total corpus size so this small graph is explored
# exhaustively (removes HNSW's normal approximate-search variance).
HNSW_EF_EXHAUSTIVE = 2000

# Two OTHER genuinely-relevant head chunks ranked at the very top (ranks 1-2)
# -- these exist purely so the final top-3 needs a THIRD result, which is
# where the target either does or doesn't get to compete.
NUM_TOP_GENUINE_MATCHES = 2
TOP_GENUINE_THETAS = (0.01, 0.02)

# Fillers ranked strictly BETWEEN the top genuine matches and the target --
# exactly enough (179) to push the target to absolute rank
# NUM_TOP_GENUINE_MATCHES + 179 + 1 == 182, i.e. one position past
# CAPPED_SEARCH_LIMIT (180) but comfortably inside NATURAL_SEARCH_LIMIT (360).
NUM_FILLERS_BEFORE_TARGET = 179
NUM_FILLERS_AFTER_TARGET = 200
EXPECTED_TARGET_RANK = NUM_TOP_GENUINE_MATCHES + NUM_FILLERS_BEFORE_TARGET + 1

TARGET_THETA = 1.0
THETA_EPSILON = 1e-4
FILLER_THETA_LOWER_BOUND = 0.03
FILLER_THETA_UPPER_BOUND = 3.0

BASE_COMMIT_TIMESTAMP = 1700000000
FILTER_TS_LOW = 0
FILTER_TS_HIGH = 9999999999

# HNSW chunk_index convention (temporal_point_builder.build_point_id):
# chunk_index 0 == head (message) chunk; anything else == non-head (diff).
HEAD_CHUNK_INDEX = 0
NON_HEAD_CHUNK_INDEX = 1


class _RaisesIfUsed:
    """Stand-in for embedding_provider/config_manager: the code under test
    (search() with precomputed_query_vector, and _filter_by_time_range(),
    which never touches config_manager) does not call either dependency, so
    a silently-permissive Mock() would hide a wrong assumption. Any
    attribute access here raises loudly instead, making "never invoked" a
    verified fact rather than an unchecked claim."""

    def __getattr__(self, name: str) -> None:
        raise AssertionError(
            f"embedding_provider/config_manager attribute {name!r} was "
            f"accessed, but the code under test should never need it when "
            f"precomputed_query_vector is supplied."
        )


def _unit(vector: np.ndarray) -> np.ndarray:
    return cast(np.ndarray, vector / np.linalg.norm(vector))


def _vector_at_theta(
    query_unit: np.ndarray, orth_unit: np.ndarray, theta: float
) -> np.ndarray:
    """A unit vector whose cosine similarity to query_unit is EXACTLY
    cos(theta) -- smaller theta means higher similarity (higher HNSW rank)."""
    return cast(np.ndarray, np.cos(theta) * query_unit + np.sin(theta) * orth_unit)


def _build_engineered_collection(
    tmp_path: Path,
) -> Tuple[FilesystemVectorStore, np.ndarray, str, int]:
    """Build a real SHARDED_JSON collection with deterministically-ranked
    vectors. Returns (store, query_vector, target_point_id, total_vectors)."""
    rng = np.random.default_rng(RNG_SEED)
    query_unit = _unit(rng.standard_normal(VECTOR_DIM))
    orth_raw = rng.standard_normal(VECTOR_DIM)
    orth_raw = orth_raw - np.dot(orth_raw, query_unit) * query_unit
    orth_unit = _unit(orth_raw)

    # Flakiness-investigation fix (post-#1493 code review): force
    # single-threaded HNSW insertion via the explicit hnsw_num_threads=1
    # constructor override (test-only; production never sets this).
    # hnswlib's add_items() defaults to num_threads=-1 (use every available
    # core); under concurrent insertion, HNSW's internal link-graph updates
    # race across worker threads and can occasionally leave a node ORPHANED
    # (disconnected from its expected neighbors) -- confirmed via captured
    # `HNSW_ORPHAN_REPAIR_EVENT orphan_count=N repaired=true` log lines on
    # the exact runs that failed. Even after Story #1359's finalize-time
    # repair reports success, the graph's *search* quality stays degraded
    # specifically for the "request almost the entire corpus" case all
    # three tests below exercise (prefetch_limit=total_vectors clamps
    # hnswlib's internal k down to exactly queryable_count) -- triggering
    # knn_query's `contiguous 2D array` error and a halved-k retry that
    # silently drops genuine top-ranked matches, not just this fixture's
    # razor-thin engineered boundary case. Forcing num_threads=1 makes
    # insertion order equal input order with no races, eliminating the
    # orphan and, with it, this whole failure chain -- verified empirically
    # at 120/120 passing builds versus a reproduced ~6-12% failure rate
    # with the default multi-threaded path.
    store = FilesystemVectorStore(
        base_path=tmp_path,
        use_chunks_db_for_new_collections=False,
        hnsw_num_threads=1,
    )
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")

    points = []

    def _add_point(commit_i: int, theta: float, is_head: bool) -> str:
        chunk_index = HEAD_CHUNK_INDEX if is_head else NON_HEAD_CHUNK_INDEX
        point_id = build_point_id("proj", f"hash{commit_i:05d}", chunk_index)
        vector = _vector_at_theta(query_unit, orth_unit, theta)
        points.append(
            {
                "id": point_id,
                "vector": vector.astype(np.float32).tolist(),
                "payload": {
                    "path": f"hash{commit_i:05d}.py",
                    "is_head": is_head,
                    "commit_timestamp": BASE_COMMIT_TIMESTAMP + commit_i,
                },
                "chunk_text": "synthetic chunk content",
            }
        )
        return cast(str, point_id)

    commit_i = 0
    for theta in TOP_GENUINE_THETAS:
        _add_point(commit_i, theta, is_head=True)
        commit_i += 1

    for theta in np.linspace(
        FILLER_THETA_LOWER_BOUND,
        TARGET_THETA - THETA_EPSILON,
        NUM_FILLERS_BEFORE_TARGET,
    ):
        _add_point(commit_i, float(theta), is_head=False)
        commit_i += 1

    target_point_id = _add_point(commit_i, TARGET_THETA, is_head=True)
    commit_i += 1

    for theta in np.linspace(
        TARGET_THETA + THETA_EPSILON, FILLER_THETA_UPPER_BOUND, NUM_FILLERS_AFTER_TARGET
    ):
        _add_point(commit_i, float(theta), is_head=False)
        commit_i += 1

    store.upsert_points("coll", points)
    store.end_indexing("coll")

    return store, query_unit, target_point_id, len(points)


def _run_and_filter(
    store: FilesystemVectorStore,
    query_vector: np.ndarray,
    total_vectors: int,
    search_limit: int,
) -> List[TemporalSearchResult]:
    """Real FSV.search() (legacy SHARDED_JSON hydration) + REAL
    TemporalSearchService._filter_by_time_range() (production is_head
    post-filter for chunk_type=commit_message) + the same score-sort +
    [:DISPLAY_LIMIT] truncation query_temporal() itself performs."""
    filter_conditions = {
        "must": [
            {
                "key": "commit_timestamp",
                "range": {"gte": FILTER_TS_LOW, "lte": FILTER_TS_HIGH},
            }
        ]
    }
    raw_results = store.search(
        query="unused",
        embedding_provider=_RaisesIfUsed(),
        collection_name="coll",
        limit=search_limit,
        filter_conditions=filter_conditions,
        precomputed_query_vector=query_vector.tolist(),
        lazy_load=True,
        prefetch_limit=total_vectors,
        ef=HNSW_EF_EXHAUSTIVE,
    )

    service = TemporalSearchService(
        config_manager=_RaisesIfUsed(),
        project_root=Path("/fake/repo"),
        vector_store_client=store,
        embedding_provider=_RaisesIfUsed(),
        collection_name="coll",
    )
    temporal_results, _ = service._filter_by_time_range(
        semantic_results=raw_results,
        start_date=ALL_TIME_RANGE[0],
        end_date=ALL_TIME_RANGE[1],
        chunk_type="commit_message",
    )
    top_results = sorted(temporal_results, key=lambda r: r.score, reverse=True)[
        :DISPLAY_LIMIT
    ]
    return top_results


@pytest.fixture
def engineered_collection(
    tmp_path: Path,
) -> Tuple[FilesystemVectorStore, np.ndarray, str, int]:
    return _build_engineered_collection(tmp_path)


def _target_path(target_point_id: str) -> str:
    return target_point_id.split(":")[2] + ".py"


def test_target_rank_is_engineered_correctly(
    engineered_collection: Tuple[FilesystemVectorStore, np.ndarray, str, int],
) -> None:
    """Sanity check on the fixture itself: the target's TRUE cosine-similarity
    rank must be EXACTLY where this test claims (182) -- strictly between
    CAPPED_SEARCH_LIMIT (180) and NATURAL_SEARCH_LIMIT (360) -- or the rest
    of this module's claims are meaningless.

    Verified via an EXACT, deterministic brute-force cosine-similarity sort
    of the raw vectors read directly off disk (scroll_points, pure JSON
    reads) -- no HNSW, no approximation, no threading -- rather than routing
    through the approximate, occasionally graph-topology-varying HNSW search
    path (store.search()). A sanity check on analytically-constructed
    vectors' true similarity ordering should be verified via exact math, not
    an approximate nearest-neighbor index: this makes THIS specific test
    100% deterministic by construction, independent of hnswlib's internal
    behavior entirely (belt-and-suspenders on top of the fixture's
    hnsw_num_threads=1 override, which independently makes the *other* two
    tests' real HNSW search deterministic)."""
    store, query_vector, target_point_id, total_vectors = engineered_collection

    all_points, _ = store.scroll_points(
        "coll",
        limit=total_vectors * 2,
        with_payload=False,
        with_vectors=True,
    )
    assert len(all_points) == total_vectors

    query_unit = query_vector / np.linalg.norm(query_vector)
    similarities: List[Tuple[str, float]] = []
    for point in all_points:
        vector = np.asarray(point["vector"], dtype=np.float64)
        vector_unit = vector / np.linalg.norm(vector)
        similarities.append((point["id"], float(np.dot(query_unit, vector_unit))))

    ranked_ids = [
        point_id
        for point_id, _ in sorted(similarities, key=lambda item: item[1], reverse=True)
    ]
    target_rank = ranked_ids.index(target_point_id) + 1

    assert target_rank == EXPECTED_TARGET_RANK == 182
    assert CAPPED_SEARCH_LIMIT < target_rank <= NATURAL_SEARCH_LIMIT


def test_natural_uncapped_search_limit_includes_the_target(
    engineered_collection: Tuple[FilesystemVectorStore, np.ndarray, str, int],
) -> None:
    """With the PRE-#1493 natural (uncapped) search_limit=360, the target's
    rank (182) is well within the candidate window, so it is one of the
    correct top-3 relevant results."""
    store, query_vector, target_point_id, total_vectors = engineered_collection

    top_results = _run_and_filter(
        store, query_vector, total_vectors, NATURAL_SEARCH_LIMIT
    )

    top_paths = [r.metadata["path"] for r in top_results]
    assert len(top_results) == DISPLAY_LIMIT
    assert _target_path(target_point_id) in top_paths


def test_capped_search_limit_drops_the_target(
    engineered_collection: Tuple[FilesystemVectorStore, np.ndarray, str, int],
) -> None:
    """With AC1's capped search_limit=180, the target's rank (182) falls
    OUTSIDE the candidate window entirely -- it is never even considered by
    the is_head post-filter, so the query returns FEWER than the requested
    3 results (2, not 3). This is the real, reproducible instance of the
    SHARDED_JSON recall risk the code review asked to see demonstrated: for
    an adversarial rank distribution (a genuine match ranked behind a
    windowful of unrelated filler with no other genuine matches nearby),
    the cap CAN cause a real result to be dropped. This is documented, not
    hidden -- see the module docstring for why real-world (real-embedding)
    testing across 7 natural-language queries did not reproduce this
    failure mode."""
    store, query_vector, target_point_id, total_vectors = engineered_collection

    top_results = _run_and_filter(
        store, query_vector, total_vectors, CAPPED_SEARCH_LIMIT
    )

    top_paths = [r.metadata["path"] for r in top_results]
    assert len(top_results) == NUM_TOP_GENUINE_MATCHES  # 2, not the requested 3
    assert _target_path(target_point_id) not in top_paths
