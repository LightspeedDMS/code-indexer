"""Unit tests for the TemporalMetadataBackend Protocol (Bug #1313 Step 2).

Defines the storage-engine-agnostic contract that both the SQLite backend
(CLI/solo) and the PostgreSQL backend (cluster) must satisfy. Mirrors the
GlobalReposBackend Protocol pattern (server/storage/protocols.py) but lives in
the CORE layer since TemporalMetadataStore itself is core, not server-only.
"""


class TestTemporalMetadataBackendProtocolShape:
    """Verify the Protocol declares exactly the required method surface."""

    def test_is_runtime_checkable(self):
        from code_indexer.storage.temporal_metadata_backend import (
            TemporalMetadataBackend,
        )

        # runtime_checkable protocols expose _is_runtime_protocol = True
        assert getattr(TemporalMetadataBackend, "_is_runtime_protocol", False) is True

    def test_declares_all_required_methods(self):
        from code_indexer.storage.temporal_metadata_backend import (
            TemporalMetadataBackend,
        )

        required = [
            "save_metadata_batch",
            "save_metadata",
            "checkpoint_wal",
            "get_point_id",
            "get_metadata",
            "delete_metadata",
            "cleanup_stale_metadata",
            "count_entries",
        ]
        for method_name in required:
            assert hasattr(TemporalMetadataBackend, method_name), (
                f"Protocol missing method: {method_name}"
            )

    def test_generate_hash_prefix_is_not_on_the_protocol(self):
        """generate_hash_prefix is a shared MODULE function, not a per-backend
        Protocol method — backends must not be required to implement it."""
        from code_indexer.storage.temporal_metadata_backend import (
            TemporalMetadataBackend,
        )

        # Protocol.__protocol_attrs__ (3.9 fallback: use dir() diff against object)
        attrs = set(dir(TemporalMetadataBackend)) - set(dir(object))
        assert "generate_hash_prefix" not in attrs

    def test_conforming_fake_satisfies_isinstance_check(self):
        """A structurally-conforming class must pass isinstance() (PEP 544)."""
        from code_indexer.storage.temporal_metadata_backend import (
            TemporalMetadataBackend,
        )

        class _FakeBackend:
            def save_metadata_batch(self, rows):
                return []

            def save_metadata(self, point_id, payload):
                return ""

            def checkpoint_wal(self):
                return None

            def get_point_id(self, hash_prefix):
                return None

            def get_metadata(self, hash_prefix):
                return None

            def delete_metadata(self, hash_prefix):
                return None

            def cleanup_stale_metadata(self, valid_hash_prefixes):
                return 0

            def count_entries(self):
                return 0

        assert isinstance(_FakeBackend(), TemporalMetadataBackend)

    def test_non_conforming_object_fails_isinstance_check(self):
        from code_indexer.storage.temporal_metadata_backend import (
            TemporalMetadataBackend,
        )

        class _Incomplete:
            def save_metadata(self, point_id, payload):
                return ""

        assert not isinstance(_Incomplete(), TemporalMetadataBackend)
