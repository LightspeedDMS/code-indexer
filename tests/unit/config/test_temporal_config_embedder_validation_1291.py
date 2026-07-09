"""Story #1291 AC11: active_embedder membership validation + removal-does-not-
auto-delete semantics.

- active_embedder MUST be a member of embedders (validated at config load) --
  this validator already exists in TemporalConfig (Story #1290) but had no
  dedicated regression test; this file locks it in for Story #1291.
- Removing an embedder from temporal.embedders does NOT auto-delete that
  embedder's on-disk collections -- proven by exercising blank-out (the only
  mechanism that ever deletes temporal collections) and showing it is
  entirely config-independent: it only inspects the v2 structure marker on
  disk, never config.temporal.embedders.
"""

import pytest

from code_indexer.config import TemporalConfig


class TestActiveEmbedderMembershipValidation:
    def test_active_embedder_not_in_embedders_raises(self):
        with pytest.raises(ValueError, match="active_embedder"):
            TemporalConfig(
                embedders=["voyage-context-4"],
                active_embedder="embed-v4.0",
            )

    def test_active_embedder_in_embedders_is_valid(self):
        config = TemporalConfig(
            embedders=["voyage-context-4", "embed-v4.0"],
            active_embedder="embed-v4.0",
        )
        assert config.active_embedder == "embed-v4.0"
        assert "embed-v4.0" in config.embedders

    def test_default_config_is_valid(self):
        """Default TemporalConfig() (single voyage-context-4 embedder) must
        still construct without error."""
        config = TemporalConfig()
        assert config.active_embedder in config.embedders


class TestRemovalDoesNotAutoDelete:
    """Removing an embedder from temporal.embedders does NOT auto-delete its
    collections -- this is documented, deliberate, separate maintenance."""

    def test_blank_out_never_reads_config_temporal_embedders(self, tmp_path):
        """blank_out_legacy_temporal_collections is the ONLY mechanism that
        ever deletes a temporal collection wholesale, and it takes NO config
        argument at all -- it cannot possibly react to an embedder being
        removed from temporal.embedders."""
        import inspect
        from code_indexer.services.temporal.temporal_blank_out import (
            blank_out_legacy_temporal_collections,
        )

        sig = inspect.signature(blank_out_legacy_temporal_collections)
        assert list(sig.parameters.keys()) == ["index_path"], (
            "blank_out_legacy_temporal_collections must take ONLY index_path "
            "-- it must never be config-aware, since config-awareness would "
            "risk deleting a removed embedder's collections."
        )

    def test_removed_embedders_v2_collection_survives_blank_out(self, tmp_path):
        """A v2-marked collection belonging to an embedder no longer listed
        in temporal.embedders must survive blank-out untouched (it is NOT
        legacy -- it has a valid v2 marker; blank-out only inspects the
        marker, never config.temporal.embedders)."""
        from code_indexer.services.temporal.temporal_blank_out import (
            blank_out_legacy_temporal_collections,
        )
        from code_indexer.services.temporal.temporal_structure_marker import (
            write_structure_marker,
        )

        # Simulate: embed-v4.0 was removed from temporal.embedders, but its
        # quarterly shard from a previous run is still on disk with a valid
        # v2 marker.
        removed_embedder_shard = tmp_path / "code-indexer-temporal-embed_v4_0-2024Q2"
        removed_embedder_shard.mkdir(parents=True)
        write_structure_marker(removed_embedder_shard, "embed_v4_0")
        (removed_embedder_shard / "hnsw_index.bin").write_bytes(b"fake_hnsw_data")

        deleted = blank_out_legacy_temporal_collections(tmp_path)

        assert deleted == []
        assert removed_embedder_shard.exists(), (
            "AC11: removing an embedder from temporal.embedders must NOT "
            "auto-delete its (v2-marked) collections -- they survive as "
            "separate, deliberate maintenance."
        )
