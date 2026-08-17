"""RED/GREEN tests for Bug #1579: shifted-duplicate point_id on re-upsert.

FilesystemVectorStore.upsert_points derives a record's on-disk DIRECTORY from
a quantized projection of the embedding vector, but the FILENAME from
point_id. When the same point_id is re-upserted with a marginally different
vector, it can quantize to a DIFFERENT directory, and the OLD file at the old
directory was never deleted -- producing two on-disk files sharing the same
point_id ("a shifted duplicate").

These tests use a REAL FilesystemVectorStore, real create_collection/
begin_indexing/upsert_points -- no mocking of the store under test. To
deterministically find two vectors that quantize to DIFFERENT top-level
directory prefixes (the directory-defining segments, i.e. segments[:-1] from
VectorQuantizer._split_hex_path), the test drives the real production
quantizer methods directly against candidate random vectors. This is
legitimate test-fixture construction (using the real methods to select
deterministic inputs), not a reimplementation of the logic under test.
"""

import json

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 1536
VECTOR_SEARCH_SEED_LIMIT = 20
PERTURBATION_SEED_OFFSET = 1000
PERTURBATION_NOISE_SCALE = 1e-6


def _dir_prefix_for(store: FilesystemVectorStore, collection_path, vector: np.ndarray):
    """Compute the directory-defining hex path prefix for `vector` using the
    REAL production quantizer pipeline (matrix + quantization range already
    persisted by create_collection). Test-fixture helper only -- not a
    reimplementation of upsert_points' own directory-derivation logic, which
    is exercised for real by the code under test.
    """
    projection_matrix = store.matrix_manager.load_matrix(collection_path)
    min_val, max_val = store._load_quantization_range("coll")
    reduced = vector @ projection_matrix
    bits = store.quantizer._quantize_to_2bit(reduced, min_val, max_val)
    hex_path = store.quantizer._bits_to_hex(bits)
    return tuple(store.quantizer._split_hex_path(hex_path)[:-1])


def _vector_for_seed(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(VECTOR_DIM)


@pytest.fixture
def store_and_paths(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path, project_root=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = tmp_path / "coll"
    return store, collection_path


def _build_shifted_pair(store, collection_path):
    """Find (vector_a, seed_a, prefix_a) and (vector_b, seed_b, prefix_b)
    whose directory prefixes genuinely differ. Fails loudly (not a silent
    retry-forever) if the bounded seed search isn't enough -- that would
    itself indicate a test-construction bug worth investigating.
    """
    vector_a = _vector_for_seed(0)
    prefix_a = _dir_prefix_for(store, collection_path, vector_a)

    for seed in range(1, VECTOR_SEARCH_SEED_LIMIT):
        candidate = _vector_for_seed(seed)
        prefix = _dir_prefix_for(store, collection_path, candidate)
        if prefix != prefix_a:
            return (vector_a, 0, prefix_a), (candidate, seed, prefix)

    raise AssertionError(
        f"Could not find two candidate vectors (seeds 0-{VECTOR_SEARCH_SEED_LIMIT - 1}) "
        "with differing directory prefixes -- test-fixture construction issue, "
        "not a production flake to silently retry."
    )


def _perturb_vector_with_same_prefix(store, collection_path, base_vector, target_prefix):
    """Return a vector that DIFFERS from base_vector in content but quantizes
    to the SAME directory prefix. A tiny perturbation of base_vector is
    overwhelmingly likely to land in the same (wide) quantization bucket for
    every directory-defining segment; bounded retry with a fresh perturbation
    seed handles the rare near-boundary case. Fails loud if never found --
    a test-construction issue, not a production flake to silently retry.
    """
    for seed in range(VECTOR_SEARCH_SEED_LIMIT):
        noise = (
            np.random.default_rng(PERTURBATION_SEED_OFFSET + seed).standard_normal(
                VECTOR_DIM
            )
            * PERTURBATION_NOISE_SCALE
        )
        candidate = base_vector + noise
        prefix = _dir_prefix_for(store, collection_path, candidate)
        if prefix == target_prefix:
            return candidate

    raise AssertionError(
        f"Could not find a perturbed vector (tries 0-{VECTOR_SEARCH_SEED_LIMIT - 1}) "
        "reproducing the SAME directory prefix as vector_b -- test-fixture "
        "construction issue."
    )


def _assert_single_surviving_record(
    store, collection_path, point_id, file_path, expected_vector
):
    """Shared assertion helper: exactly one vector_*.json exists for this
    collection, its content matches expected_vector, and get_point() agrees.
    """
    all_files = list(collection_path.rglob("vector_*.json"))
    assert len(all_files) == 1, (
        f"Bug #1579: expected exactly ONE vector_*.json file for point_id "
        f"'{point_id}', found {len(all_files)}: {[str(f) for f in all_files]}"
    )

    with open(all_files[0]) as f:
        data = json.load(f)
    assert data["id"] == point_id
    assert data["payload"]["path"] == file_path
    np.testing.assert_allclose(
        np.array(data["vector"], dtype=np.float64), expected_vector, atol=1e-6
    )

    point = store.get_point(point_id, "coll")
    assert point is not None
    np.testing.assert_allclose(
        np.array(point["vector"], dtype=np.float64), expected_vector, atol=1e-6
    )


class TestShiftedDuplicateOnReupsert:
    def test_reupsert_with_relocating_vector_leaves_exactly_one_file(
        self, store_and_paths
    ):
        """RED (pre-fix): re-upserting the same point_id with a vector that
        quantizes to a DIFFERENT directory leaves TWO vector_*.json files on
        disk sharing the same point_id (the old one never gets deleted).
        GREEN (post-fix): exactly one file remains, at the NEW location, with
        content reflecting the NEW vector.
        """
        store, collection_path = store_and_paths
        (vector_a, _, prefix_a), (vector_b, _, prefix_b) = _build_shifted_pair(
            store, collection_path
        )
        assert prefix_a != prefix_b, "fixture construction must find differing prefixes"

        store.begin_indexing("coll")
        store.upsert_points(
            "coll",
            [{"id": "p_shift", "vector": vector_a, "payload": {"path": "src/a.py"}}],
        )
        first_files = list(collection_path.rglob("vector_*.json"))
        assert len(first_files) == 1, "sanity: exactly one file after first upsert"

        store.upsert_points(
            "coll",
            [{"id": "p_shift", "vector": vector_b, "payload": {"path": "src/a.py"}}],
        )

        _assert_single_surviving_record(
            store, collection_path, "p_shift", "src/a.py", vector_b
        )

    def test_reupsert_staying_in_same_directory_leaves_exactly_one_file(
        self, store_and_paths
    ):
        """Companion regression test: an ordinary in-place overwrite (vector
        changes but stays in the SAME directory) must still leave exactly one
        file -- no accidental extra unlink attempts introduced by the fix.
        """
        store, collection_path = store_and_paths
        (vector_a, _, prefix_a), (vector_b, _, prefix_b) = _build_shifted_pair(
            store, collection_path
        )
        vector_c = _perturb_vector_with_same_prefix(
            store, collection_path, vector_b, prefix_b
        )
        assert not np.allclose(vector_b, vector_c), "vector_c must genuinely differ"

        store.begin_indexing("coll")
        store.upsert_points(
            "coll",
            [{"id": "p_stay", "vector": vector_b, "payload": {"path": "src/b.py"}}],
        )
        store.upsert_points(
            "coll",
            [{"id": "p_stay", "vector": vector_c, "payload": {"path": "src/b.py"}}],
        )

        _assert_single_surviving_record(
            store, collection_path, "p_stay", "src/b.py", vector_c
        )
