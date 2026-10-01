"""Unit tests for Bug #1969 code review finding F2 (P3): the internal
IDIndexManager().rebuild_from_vectors() call inside
_rebuild_derived_artifacts() (collection_dedup_repair.py) must NEVER
self-heal.

Bug #1969's fix added a one-shot self-heal to IDIndexManager.
rebuild_from_vectors() (self_heal=True opts in). repair_duplicate_and_
shifted_points() itself calls rebuild_from_vectors() internally, as its
final "rebuild id_index.bin + HNSW from the repaired truth" step
(_rebuild_derived_artifacts, collection_dedup_repair.py:1324). Code review
found that if this nested call ALSO self-healed, and its own scan (over
the now-supposedly-clean tree) still found a duplicate point_id -- e.g. a
renumber-introduced collision, or any other reason the outer repair pass
left one behind -- it would recursively trigger a SECOND
repair_duplicate_and_shifted_points() call from inside the first one's own
derived-artifacts rebuild. This was reachable, undocumented, untested, and
silently changed behavior for scroll_points()/consolidate_collection_
in_place() callers too (their nested rebuild used to fail loudly on this
condition, not silently recurse).

Fix: _rebuild_derived_artifacts()'s internal call passes self_heal=False
explicitly -- a nested rebuild during a repair NEVER attempts a second
repair; if it still finds a duplicate, it raises DuplicateSourceIdError
immediately, exactly like it did before Bug #1969's fix existed.
"""

import json
from pathlib import Path

import pytest

from code_indexer.storage.id_index_manager import DuplicateSourceIdError, IDIndexManager
import code_indexer.storage.shared.collection_dedup_repair as repair_mod


class TestRebuildDerivedArtifactsNeverSelfHeals:
    """Direct proof that _rebuild_derived_artifacts() wires self_heal=False
    into its internal IDIndexManager().rebuild_from_vectors() call."""

    def test_internal_rebuild_call_passes_self_heal_false(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        captured_kwargs = {}
        original_rebuild = IDIndexManager.rebuild_from_vectors

        def _spy_rebuild(self, collection_path, **kwargs):
            captured_kwargs.update(kwargs)
            return original_rebuild(self, collection_path, **kwargs)

        monkeypatch.setattr(IDIndexManager, "rebuild_from_vectors", _spy_rebuild)

        # Minimal clean (no duplicate) collection -- only the wiring of the
        # internal call's kwargs is under test here. collection_meta.json
        # is required by the downstream HNSW rebuild step this function
        # also performs.
        (tmp_path / "vector_a.json").write_text(
            json.dumps({"id": "p1", "vector": [0.1, 0.2]})
        )
        (tmp_path / "collection_meta.json").write_text(
            json.dumps(
                {
                    "name": "coll",
                    "vector_size": 2,
                    "hnsw_index": {
                        "version": 1,
                        "vector_dim": 2,
                        "space": "cosine",
                        "vector_count": 0,
                        "id_mapping": {},
                    },
                }
            )
        )

        repair_mod._rebuild_derived_artifacts(tmp_path, vector_dim=2, space="cosine")

        assert "self_heal" in captured_kwargs, (
            "_rebuild_derived_artifacts must pass self_heal explicitly to "
            "its internal rebuild_from_vectors() call -- relying on the "
            "default alone is not the reviewer-requested fix"
        )
        assert captured_kwargs["self_heal"] is False


class TestRebuildDerivedArtifactsRaisesInsteadOfRecursing:
    """If the tree _rebuild_derived_artifacts() rebuilds from STILL
    contains a duplicate point_id (simulating the exact re-entrancy hazard
    the reviewer found), it must raise DuplicateSourceIdError immediately
    -- NEVER trigger a second repair_duplicate_and_shifted_points() call."""

    def test_duplicate_in_tree_raises_without_a_second_repair_attempt(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # A duplicate point_id already present in the tree
        # _rebuild_derived_artifacts is asked to rebuild from -- exactly
        # the shape its OWN internal rebuild_from_vectors() scan would
        # encounter if the outer repair pass somehow left one behind.
        (tmp_path / "vector_a.json").write_text(
            json.dumps({"id": "dup-id", "vector": [1.0]})
        )
        (tmp_path / "vector_b.json").write_text(
            json.dumps({"id": "dup-id", "vector": [2.0]})
        )

        call_count = {"n": 0}
        original_repair = repair_mod.repair_duplicate_and_shifted_points

        def _counting_repair(*args, **kwargs):
            call_count["n"] += 1
            return original_repair(*args, **kwargs)

        monkeypatch.setattr(
            repair_mod, "repair_duplicate_and_shifted_points", _counting_repair
        )

        with pytest.raises(DuplicateSourceIdError):
            repair_mod._rebuild_derived_artifacts(
                tmp_path, vector_dim=4, space="cosine"
            )

        assert call_count["n"] == 0, (
            "a duplicate found during _rebuild_derived_artifacts()'s own "
            "internal rebuild must never trigger ANOTHER "
            "repair_duplicate_and_shifted_points() call -- recursion must "
            "be impossible, not merely bounded"
        )
