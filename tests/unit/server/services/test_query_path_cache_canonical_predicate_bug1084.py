"""query_path_cache predicate unification (Bug #1084 B4).

``is_immutable_versioned_snapshot`` is the ONLY gate that may route a key to a
NO-TTL cache. Phase B replaces its PRIVATE ``.versioned`` decision logic with the
single canonical predicate from ``snapshot_paths`` (CANONICAL clause only):

- Canonical ``.versioned/{ns}/v_*`` paths (local AND cow-daemon under the mount)
  are genuinely immutable -> True / NO-TTL.
- A subpath INSIDE a canonical snapshot stays immutable -> True (preserves the
  Story #1082 subpath contract).
- LEGACY cow shapes (``{mount}/{ns}/v_*``, no ``.versioned``) and flat ONTAP
  (``{mount}/v_*``) stay False / SHORT-TTL: they predate retention and could be
  deleted, so the NO-TTL immutability promise must NOT apply to them.

The decision is delegated to the canonical module (single source of truth, AC #7);
no private ``.versioned`` membership test remains in query_path_cache.
"""

from __future__ import annotations

from code_indexer.server.services.query_path_cache import (
    is_immutable_versioned_snapshot,
)


class TestCanonicalSnapshotsAreImmutable:
    def test_canonical_cow_snapshot_under_mount_is_immutable(self):
        # Cow-daemon canonical snapshot now legitimately gains NO-TTL caching.
        assert is_immutable_versioned_snapshot(
            "/mnt/cow-storage/.versioned/flask/v_1717000000"
        )

    def test_canonical_local_snapshot_is_immutable(self):
        assert is_immutable_versioned_snapshot(
            "/data/golden-repos/.versioned/flask/v_1700000000"
        )

    def test_dotted_alias_canonical_cow_snapshot_is_immutable(self):
        assert is_immutable_versioned_snapshot(
            "/mnt/cow-storage/.versioned/"
            "langfuse_Claude_Code_seba_battig_lightspeeddms_com/v_1717000000"
        )

    def test_subpath_inside_canonical_snapshot_stays_immutable(self):
        # Story #1082 contract: a deeper path inside the snapshot is immutable.
        assert is_immutable_versioned_snapshot(
            "/data/golden-repos/.versioned/my-repo/v_42/.code-indexer/config.json"
        )
        assert is_immutable_versioned_snapshot(
            "/mnt/cow-storage/.versioned/flask/v_42/.code-indexer/scip/index.scip.db"
        )


class TestLegacyAndMutableStaySoftTTL:
    def test_legacy_cow_shape_is_not_immutable(self):
        # Legacy cow snapshot (no .versioned) predates retention -> SHORT-TTL.
        assert not is_immutable_versioned_snapshot(
            "/mnt/cow-storage/flask/v_1717000000"
        )

    def test_flat_ontap_shape_is_not_immutable(self):
        assert not is_immutable_versioned_snapshot("/mnt/fsx/v_1717000000")

    def test_mutable_base_clone_is_not_immutable(self):
        assert not is_immutable_versioned_snapshot("/data/golden-repos/flask")

    def test_activated_cow_clone_is_not_immutable(self):
        assert not is_immutable_versioned_snapshot(
            "/mnt/cow-storage/activated-repos/alice/flask"
        )

    def test_traversal_token_rejected(self):
        assert not is_immutable_versioned_snapshot(
            "/data/golden-repos/.versioned/../flask/v_1"
        )

    def test_empty_path_rejected(self):
        assert not is_immutable_versioned_snapshot("")


class TestSingleSourceDelegation:
    def test_predicate_delegates_to_canonical_module(self, monkeypatch):
        """The cache predicate must route its decision through snapshot_paths.

        We patch the canonical predicate to a sentinel and assert the cache
        predicate's result tracks it -- proving there is no independent private
        ``.versioned`` decision left in query_path_cache (AC #7 single source).
        """
        import code_indexer.server.services.query_path_cache as qpc

        calls = []

        def fake_canon(path, *, mount_point=None):
            calls.append((path, mount_point))
            return True

        monkeypatch.setattr(qpc, "_canonical_is_versioned_snapshot", fake_canon)

        # Even a base-clone path returns True now, because the decision is
        # delegated to our patched canonical predicate.
        assert qpc.is_immutable_versioned_snapshot("/data/golden-repos/flask") is True
        assert calls, "cache predicate must call the canonical predicate"
