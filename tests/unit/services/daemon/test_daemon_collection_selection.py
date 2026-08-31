"""
Test for daemon collection selection bug.

REGRESSION BUG: Daemon loads collections alphabetically (collections[0]) and picks
the FIRST one. When 'code-indexer-temporal' exists alongside 'voyage-code-3',
the daemon incorrectly loads the temporal collection for semantic queries.

ROOT CAUSE: _load_semantic_indexes() uses collections[0] which loads alphabetically-first
collection instead of identifying the main (non-temporal) collection.

This test MUST fail with current code and pass after fix.
"""

import tempfile
import shutil
from pathlib import Path


def test_daemon_selects_main_collection_not_temporal():
    """
    Test that daemon selects MAIN collection when multiple collections exist.

    SCENARIO: Project has both temporal and main collections:
    - code-indexer-temporal (temporal queries)
    - voyage-code-3 (main semantic queries)

    CURRENT BUG: Daemon uses collections[0], which alphabetically selects
    'code-indexer-temporal' first, breaking semantic queries.

    EXPECTED: Daemon should identify and load the MAIN collection (voyage-code-3),
    excluding temporal collections from consideration.

    This test reproduces the exact bug from evolution repository.
    """
    # Create temporary project directory
    temp_dir = tempfile.mkdtemp()
    try:
        project_path = Path(temp_dir)
        code_indexer_dir = project_path / ".code-indexer"
        index_dir = code_indexer_dir / "index"
        index_dir.mkdir(parents=True)

        # Bug #1730: _load_semantic_indexes now resolves the CONFIGURED
        # collection (via a real config.json) before selecting one, rather
        # than blindly using an arbitrary list_collections() order. The
        # default config's embedding model resolves to "voyage-code-3",
        # matching this test's main_collection below.
        from code_indexer.config import ConfigManager

        ConfigManager(code_indexer_dir / "config.json").create_default_config(
            codebase_dir=project_path
        )

        # Create TWO collections: temporal (alphabetically first) and main (alphabetically second)
        temporal_collection = index_dir / "code-indexer-temporal"
        main_collection = index_dir / "voyage-code-3"

        temporal_collection.mkdir()
        main_collection.mkdir()

        # Create minimal metadata and HNSW index for both collections
        import json
        import numpy as np

        for collection_path in [temporal_collection, main_collection]:
            # Create collection metadata
            metadata = {
                "name": collection_path.name,
                "vector_size": 1024,
                "created_at": "2025-01-01T00:00:00",
            }
            with open(collection_path / "collection_meta.json", "w") as f:
                json.dump(metadata, f)

            # Create minimal HNSW index (required for daemon to load successfully)
            # Use HNSWIndexManager to create a valid index
            from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

            hnsw_manager = HNSWIndexManager(vector_dim=1024, space="cosine")

            # Add one dummy vector so index isn't empty
            dummy_vector = np.array([0.1] * 1024, dtype=np.float32)
            vectors = np.array([dummy_vector], dtype=np.float32)
            vector_ids = [0]  # Regular Python list of ints for JSON serialization

            # Build and save the index
            hnsw_manager.build_index(
                collection_path=collection_path, vectors=vectors, ids=vector_ids
            )

            # Create minimal ID index
            from code_indexer.storage.id_index_manager import IDIndexManager

            id_manager = IDIndexManager()
            id_index = {"0": collection_path / "vectors" / "dummy.json"}
            id_manager.save_index(collection_path, id_index)

        # Get list of collections to verify alphabetical ordering (the bug trigger)
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        vector_store = FilesystemVectorStore(
            base_path=index_dir, project_root=project_path
        )
        collections = vector_store.list_collections()

        # Verify test setup: temporal collection is alphabetically first (this triggers the bug)
        assert collections[0] == "code-indexer-temporal", (
            "Test setup verification: temporal collection should be alphabetically first"
        )
        assert collections[1] == "voyage-code-3", (
            "Test setup verification: main collection should be alphabetically second"
        )

        # Initialize daemon service and load indexes
        from code_indexer.daemon.service import CIDXDaemonService
        from code_indexer.daemon.cache import CacheEntry

        service = CIDXDaemonService()
        cache_entry = CacheEntry(project_path, ttl_minutes=10)

        # Load semantic indexes. Pre-#1730, this loaded collections[0] =
        # 'code-indexer-temporal' (alphabetically first). Post-#1730, the
        # CONFIGURED collection is resolved from the real config.json
        # above, which never considers "code-indexer-temporal" a candidate.
        service._load_semantic_indexes(cache_entry)

        assert cache_entry.hnsw_index is not None, (
            "Daemon should have loaded an HNSW index"
        )
        assert cache_entry.collection_name == "voyage-code-3", (
            f"Daemon should have loaded the MAIN collection 'voyage-code-3', "
            f"not the temporal one, but loaded "
            f"'{cache_entry.collection_name}'"
        )

        # For now, this test documents the bug and will be enhanced after implementing the fix

    finally:
        shutil.rmtree(temp_dir)
