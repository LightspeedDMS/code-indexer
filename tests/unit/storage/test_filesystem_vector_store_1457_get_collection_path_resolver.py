"""FilesystemVectorStore._get_collection_path per-instance-gated resolver
support (Story #1457 AC8).

Resolution MUST be GATED PER-STORE-INSTANCE on whether that
FilesystemVectorStore was constructed WITH a TemporalShardResolver
injected. A store instance WITHOUT a resolver behaves byte-identically to
today (direct `self.base_path / collection_name`); this is an explicit
dual-mode design, never a blanket replacement -- without this gate the
write/index path would incorrectly try to resolve builds against the
published immutable sister snapshot instead of writing to its own
build-in-progress location.
"""

from __future__ import annotations

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def test_no_resolver_injected_is_byte_identical_even_for_temporal_name(tmp_path):
    """Without an injected resolver, ANY collection name (including one
    that looks temporal) resolves via direct construction -- unchanged."""
    store = FilesystemVectorStore(base_path=tmp_path)

    result = store._get_collection_path("code-indexer-temporal-voyage_code_3-2024Q1")

    assert result == tmp_path / "code-indexer-temporal-voyage_code_3-2024Q1"


def test_resolver_injected_non_temporal_name_uses_direct_construction(tmp_path):
    """A non-temporal collection name is unaffected even when a resolver IS
    injected -- the gate is per-name (temporal vs. not), not just
    per-instance."""
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.services.temporal.temporal_shard_resolver import (
        TemporalShardResolver,
    )

    alias_manager = AliasManager(str(tmp_path / "aliases"))
    resolver = TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=tmp_path / "sister",
        legacy_index_path=tmp_path / "index",
    )
    store = FilesystemVectorStore(
        base_path=tmp_path / "index", temporal_shard_resolver=resolver
    )

    result = store._get_collection_path("my_semantic_collection")

    assert result == (tmp_path / "index") / "my_semantic_collection"


def test_resolver_injected_temporal_name_resolves_via_sister_pointer(tmp_path):
    """Positive path: a resolver IS injected, the collection name parses as
    temporal, AND a sister pointer exists -- _get_collection_path redirects
    to the resolved sister path instead of the in-repo base_path."""
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.services.temporal.temporal_shard_resolver import (
        TemporalShardResolver,
    )

    alias_manager = AliasManager(str(tmp_path / "aliases"))
    sister_version_dir = (
        tmp_path
        / "sister"
        / ".versioned"
        / "evolution-temporal-voyage_code_3-2024Q1"
        / "v_1700000000"
    )
    alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(sister_version_dir)
    )
    resolver = TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=tmp_path / "sister",
        legacy_index_path=tmp_path / "index",
    )
    store = FilesystemVectorStore(
        base_path=tmp_path / "index", temporal_shard_resolver=resolver
    )

    result = store._get_collection_path("code-indexer-temporal-voyage_code_3-2024Q1")

    assert result == sister_version_dir
    assert result != (tmp_path / "index") / "code-indexer-temporal-voyage_code_3-2024Q1"
