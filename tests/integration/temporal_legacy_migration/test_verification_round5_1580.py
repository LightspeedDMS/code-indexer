"""Issue #1580 round-5 fix: round-4's anchored ``endswith(".tmp")`` closed
the substring-match exploit but is now TOO STRICT for one real production
staging convention.

``services/temporal/temporal_projection_matrix.py``'s ``_atomic_replace_via_tmp``
(used for the ``projection_matrix.npy`` Bug #1242/#1264 self-heal write,
INSIDE the shard root that ``_structural_manifest`` walks) stages via:

    tmp_path = final_path.parent / f"{final_path.name}.tmp.{uuid.uuid4().hex}"

e.g. ``projection_matrix.npy.tmp.<32-lowercase-hex-chars>`` -- this does NOT
end in exactly ``.tmp`` (it has a 32-char hex suffix after ``.tmp.``), so the
round-4 anchored check wrongly rejects it as an unexpected/altered file if a
verification walk observes it mid-write (a genuine in-flight concurrent
self-heal), reproducing the ORIGINAL #1580 symptom (a legitimate in-place
refresh misclassified as a collision) for this specific real write path.

Confirmed by grepping every other real ``.tmp``-staging site under
``storage/`` and ``services/temporal/`` (``id_index_manager.py``,
``hnsw_index_manager.py``, ``background_index_rebuilder.py``,
``temporal_progressive_metadata.py``, ``temporal_structure_marker.py``,
``filesystem_vector_store.py``, ``chunk_layout.py``,
``collection_migration.py``, ``collection_dedup_repair.py``,
``hnsw_sync_state.py``): every one of those ends in exactly ``.tmp`` -- only
``temporal_projection_matrix.py`` uses the ``.tmp.<uuid4-hex>`` shape, and
only it writes inside a temporal shard root.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from code_indexer.server.services.temporal_legacy_migration.verification import (
    _is_transient_non_content_artifact,
    verify_source_subset_of_target,
)


def _make_sharded_json_shard(path: Path, point_id: str, vector: list) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "collection_meta.json").write_text('{"name": "q1"}')
    record = {"id": point_id, "vector": vector, "payload": {"source": "x"}}
    (path / f"vector_{point_id}.json").write_text(json.dumps(record))


# ---------------------------------------------------------------------------
# Core fix: the real projection-matrix uuid4-hex staging convention must be
# exempted.
# ---------------------------------------------------------------------------


def test_projection_matrix_uuid_staging_file_is_exempted():
    """RED against pre-fix code: ``_is_transient_non_content_artifact`` only
    checked ``endswith(".tmp")``, which is False for this name (it ends in
    32 hex chars, not literally ``.tmp``) -- the pre-fix assertion fails.
    """
    realistic_hex = uuid.uuid4().hex
    assert len(realistic_hex) == 32
    name = f"projection_matrix.npy.tmp.{realistic_hex}"
    assert _is_transient_non_content_artifact(name) is True


@pytest.mark.parametrize(
    "realistic_hex",
    [
        "0123456789abcdef0123456789abcdef",
        "deadbeefdeadbeefdeadbeefdeadbeef",
        "ffffffffffffffffffffffffffffffff",
        "00000000000000000000000000000000",
    ],
)
def test_projection_matrix_uuid_staging_file_is_exempted_various_hex(realistic_hex):
    """Guard against an implementation that only special-cases one literal
    example -- several realistic 32-hex-char suffixes must all be exempted.
    """
    assert (
        _is_transient_non_content_artifact(f"projection_matrix.npy.tmp.{realistic_hex}")
        is True
    )


# ---------------------------------------------------------------------------
# Guard-rail: the fix must stay precisely anchored to the real convention --
# it must NOT reopen the round-4 substring-match exploit.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "vector_a.tmpdata.json",
        "something.tmp.json",
        "vector_x.tmp_backup.json",
        # Near-misses of the new uuid-suffix shape: must still be rejected.
        "projection_matrix.npy.tmp.short",  # not 32 hex chars
        "projection_matrix.npy.tmp.0123456789abcdef0123456789abcde",  # 31 chars
        "projection_matrix.npy.tmp.0123456789abcdef0123456789abcdef0",  # 33 chars
        "projection_matrix.npy.tmp.0123456789ABCDEF0123456789ABCDEF",  # uppercase
        "projection_matrix.npy.tmp.0123456789abcdefg123456789abcdef",  # 'g' not hex
    ],
)
def test_real_content_and_near_miss_filenames_are_still_rejected(name: str):
    """The round-4 exploit fix (``vector_a.tmpdata.json``, ``something.tmp.json``
    still rejected) must remain intact, and the new uuid-suffix pattern must
    be exact -- not a loose match that would re-admit disguised content.
    """
    assert _is_transient_non_content_artifact(name) is False


def test_genuine_lock_and_bare_tmp_filenames_still_exempted_unaffected():
    """Guard-rail: the pre-existing anchored ``.lock``/``.tmp`` behavior from
    round 4 must be completely unaffected by this fix.
    """
    assert _is_transient_non_content_artifact(".metadata.lock") is True
    assert _is_transient_non_content_artifact("temporal_progress.json.tmp") is True
    assert _is_transient_non_content_artifact("vector_p1.12345.67890.tmp") is True


# ---------------------------------------------------------------------------
# Integration-level reproduction: a real in-flight projection-matrix staging
# file present only at the target (simulating a concurrent Bug #1242/#1264
# self-heal write caught mid-flight by a verification walk) must never be
# treated as an unexpected addition.
# ---------------------------------------------------------------------------


def test_inflight_projection_matrix_staging_file_does_not_trigger_false_collision(
    tmp_path: Path,
):
    """Integration-level version of the core fix, through the real
    ``verify_source_subset_of_target`` entry point Bug #1529's in-place
    refresh verification actually calls. RED against pre-fix code: the
    staging file (present only at target, never at source) is not
    recognized as expected churn/addition and trips "unexpected file(s)".
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", [1.0, 2.0, 3.0])
    _make_sharded_json_shard(target, "p1", [1.0, 2.0, 3.0])

    staging_name = f"projection_matrix.npy.tmp.{uuid.uuid4().hex}"
    (target / staging_name).write_bytes(b"partially-written-matrix-bytes")

    verify_source_subset_of_target(source, target)  # must not raise
