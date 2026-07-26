"""FilesystemVectorStore._get_collection_path pin-stack consultation
(Story #1457 AC8 Step 6).

While a `TemporalShardResolver.pin()` block is active for a given
(embedder_slug, quarter) namespace, `_get_collection_path` MUST consult the
resolver's pin stack FIRST and return the pinned path WITHOUT calling
`resolve()` again -- so a concurrent alias swap happening mid-read cannot
change which physical directory the read actually touches. This is the
mechanism that closes the in-flight-temporal-version deletion hazard: a
query holding a pin is isolated from any swap that happens during its read.

Real AliasManager, real TemporalShardResolver, real QueryTracker, real
FilesystemVectorStore -- no mocking of the code under test.
"""

from __future__ import annotations

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def test_get_collection_path_returns_pinned_path_during_active_pin(tmp_path):
    """A concurrent alias swap happening WHILE a pin() block is active must
    NOT change what _get_collection_path returns -- the read stays isolated
    to the version it was pinned against."""
    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "index"
    alias_manager = AliasManager(str(aliases_dir))

    version_old = sister_root / ".versioned" / "ns" / "v_1700000000"
    version_old.mkdir(parents=True)
    version_new = sister_root / ".versioned" / "ns" / "v_1700000001"
    version_new.mkdir(parents=True)
    pointer_namespace = "evolution-temporal-voyage_code_3-2024Q1"
    alias_manager.create_alias(pointer_namespace, str(version_old))

    query_tracker = QueryTracker()
    resolver = TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=sister_root,
        legacy_index_path=legacy_index_path,
        query_tracker=query_tracker,
    )
    store = FilesystemVectorStore(
        base_path=legacy_index_path, temporal_shard_resolver=resolver
    )
    collection_name = "code-indexer-temporal-voyage_code_3-2024Q1"

    with resolver.pin("voyage_code_3", "2024Q1"):
        # Pin acquired against version_old. A concurrent refresh now
        # publishes a NEW version via a real swap_alias call.
        alias_manager.swap_alias(pointer_namespace, str(version_new), str(version_old))

        # The pin isolates this read: _get_collection_path must STILL
        # return the pinned (old) path, not re-resolve to the new one.
        assert store._get_collection_path(collection_name) == version_old

    # Pin released -- resolution now correctly reflects the swap.
    assert store._get_collection_path(collection_name) == version_new
