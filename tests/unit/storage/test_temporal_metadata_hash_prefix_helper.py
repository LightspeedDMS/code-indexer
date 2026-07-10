"""Unit tests for the shared module-level generate_hash_prefix() helper.

Bug #1313 Step 1: the hash-prefix computation must be a single module-level
function (not duplicated per-backend) so both the SQLite backend and the
PostgreSQL backend derive filenames identically.
"""

import hashlib
import tempfile
from pathlib import Path


class TestGenerateHashPrefixModuleFunction:
    """generate_hash_prefix(point_id) -> 16-char sha256 hex prefix."""

    def test_equals_sha256_hexdigest_first_16_chars(self):
        from code_indexer.storage.temporal_metadata_store import generate_hash_prefix

        point_id = "project:diff:abc123:path/to/file.py:0"
        expected = hashlib.sha256(point_id.encode()).hexdigest()[:16]

        assert generate_hash_prefix(point_id) == expected

    def test_identical_across_repeated_calls(self):
        from code_indexer.storage.temporal_metadata_store import generate_hash_prefix

        point_id = "project:diff:def456:src/main.py:5"

        first = generate_hash_prefix(point_id)
        second = generate_hash_prefix(point_id)

        assert first == second

    def test_different_point_ids_produce_different_prefixes(self):
        from code_indexer.storage.temporal_metadata_store import generate_hash_prefix

        assert generate_hash_prefix("a") != generate_hash_prefix("b")

    def test_instance_method_forwards_to_module_function(self):
        """TemporalMetadataStore.generate_hash_prefix must forward to the
        shared module function (identical output for identical input)."""
        from code_indexer.storage.temporal_metadata_store import (
            TemporalMetadataStore,
            generate_hash_prefix,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            collection_path = Path(tmpdir) / "code-indexer-temporal"
            store = TemporalMetadataStore(collection_path)

            point_id = "project:diff:xyz:file.py:2"
            assert store.generate_hash_prefix(point_id) == generate_hash_prefix(
                point_id
            )
