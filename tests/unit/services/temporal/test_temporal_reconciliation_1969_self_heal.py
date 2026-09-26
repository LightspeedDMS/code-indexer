"""Unit tests for Bug #1969: temporal_reconciliation.py's
_reconcile_shard_legacy() calls IDIndexManager().rebuild_from_vectors()
directly (temporal_reconciliation.py:116) -- confirmed via a REAL call to
that function (not just a call-shape simulation elsewhere), per code
review finding F4 item 2: a prior test only mimicked the call SHAPE
without ever calling through this module.

_reconcile_shard_legacy() must pass self_heal=True (a genuine write/
reconcile-path caller), and the retry must still be bounded (repair
attempted at most once) even though a temporal shard's literal
"{project}:commit:{hash}:{j}" point_id scheme is structurally
incompatible with collection_dedup_repair.py's whole-collection identity
gate (md5(unique_key) == point_id) -- so a duplicate there can never
actually be resolved by the repair, but the attempt must still happen
exactly once, never loop, and the resulting DuplicateSourceIdError must
propagate out of _reconcile_shard_legacy() unchanged.
"""

import json
from pathlib import Path

import pytest

from code_indexer.services.temporal.temporal_reconciliation import (
    _reconcile_shard_legacy,
)
from code_indexer.storage.id_index_manager import DuplicateSourceIdError
import code_indexer.storage.shared.collection_dedup_repair as repair_mod


class TestReconcileShardLegacySelfHeal:
    def test_calls_rebuild_from_vectors_with_self_heal_true(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Spy on IDIndexManager.rebuild_from_vectors to confirm
        _reconcile_shard_legacy's real call passes self_heal=True -- a
        clean shard (no duplicate) so the call completes normally."""
        from code_indexer.storage.id_index_manager import IDIndexManager

        captured_kwargs = {}
        original = IDIndexManager.rebuild_from_vectors

        def _spy(self, collection_path, **kwargs):
            captured_kwargs.update(kwargs)
            return original(self, collection_path, **kwargs)

        monkeypatch.setattr(IDIndexManager, "rebuild_from_vectors", _spy)

        (tmp_path / "vector_a.json").write_text(
            json.dumps(
                {
                    "id": "myproj:commit:abc123:0",
                    "vector": [0.1],
                    "payload": {"commit_message": "m"},
                }
            )
        )

        result = _reconcile_shard_legacy(tmp_path, "myshard", [])

        assert result == []
        assert captured_kwargs.get("self_heal") is True

    def test_duplicate_point_id_raises_after_bounded_one_shot_self_heal(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A temporal-shaped duplicate point_id is NOT actually repairable
        (foreign identity scheme, gate rejects it) -- the self-heal is
        still attempted exactly once (bounded, never loops) before
        DuplicateSourceIdError propagates out of the REAL
        _reconcile_shard_legacy() call."""
        point_id = "myproj:commit:def456:0"
        (tmp_path / "vector_a.json").write_text(
            json.dumps(
                {"id": point_id, "vector": [1.0], "payload": {"commit_message": "v1"}}
            )
        )
        nested = tmp_path / "shifted"
        nested.mkdir()
        (nested / "vector_b.json").write_text(
            json.dumps(
                {"id": point_id, "vector": [2.0], "payload": {"commit_message": "v2"}}
            )
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
            _reconcile_shard_legacy(tmp_path, "myshard", [])

        assert call_count["n"] == 1, (
            "the self-heal must be attempted exactly once through the "
            "REAL _reconcile_shard_legacy() call path, never looped"
        )
