"""TemporalShardResolver (Story #1457 AC8): single authority for "logical
temporal namespace -> resolved physical path + source", pointer-first /
in-repo-fallback-second, decided PER (embedder, quarter) namespace
independently.

Bridges the two incompatible physical naming schemes: the in-repo legacy
directory is `code-indexer-temporal-{embedder_slug}[-{quarter}]` (base-name
form) while the sister alias pointer is
`{repo_alias}-temporal-{embedder_slug}[-{quarter}]` (alias-prefixed form).

These tests use a REAL AliasManager against a real tmp_path directory (never
a mock of AliasManager or the resolver's own logic).
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
    TemporalShardSource,
    parse_physical_temporal_name,
)


def _make_resolver(
    tmp_path: Path, repo_alias: str = "evolution"
) -> TemporalShardResolver:
    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "clone" / ".code-indexer" / "index"
    legacy_index_path.mkdir(parents=True)
    alias_manager = AliasManager(str(aliases_dir))
    return TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias=repo_alias,
        sister_root=sister_root,
        legacy_index_path=legacy_index_path,
    )


def test_resolve_returns_none_when_nothing_exists(tmp_path):
    resolver = _make_resolver(tmp_path)
    result = resolver.resolve("voyage_code_3", "2024Q1")
    assert result is None


def test_resolve_returns_sister_pointer_when_alias_pointer_exists(tmp_path):
    resolver = _make_resolver(tmp_path, repo_alias="evolution")
    sister_version_dir = (
        tmp_path
        / "sister"
        / ".versioned"
        / "evolution-temporal-voyage_code_3-2024Q1"
        / "v_1700000000"
    )
    resolver._alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(sister_version_dir)
    )

    result = resolver.resolve("voyage_code_3", "2024Q1")

    assert result is not None
    assert result.source == TemporalShardSource.SISTER_POINTER
    assert result.path == sister_version_dir
    assert result.pointer_namespace == "evolution-temporal-voyage_code_3-2024Q1"
    assert result.physical_name == "code-indexer-temporal-voyage_code_3-2024Q1"


def test_resolve_falls_back_to_in_repo_legacy_when_rows_exist_and_no_pointer(tmp_path):
    """No sister pointer, but the in-repo legacy shard has real committed
    rows (via the row-existence scan) -- resolve to IN_REPO_LEGACY, is_queryable
    True since hnsw_index.bin is present."""
    resolver = _make_resolver(tmp_path, repo_alias="evolution")
    legacy_shard_dir = (
        tmp_path
        / "clone"
        / ".code-indexer"
        / "index"
        / "code-indexer-temporal-voyage_code_3-2024Q1"
    )
    nested = legacy_shard_dir / "a" / "b" / "c" / "d"
    nested.mkdir(parents=True)
    (nested / "vector_abc123.json").write_text('{"point_id": "p1"}')
    (legacy_shard_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

    result = resolver.resolve("voyage_code_3", "2024Q1")

    assert result is not None
    assert result.source == TemporalShardSource.IN_REPO_LEGACY
    assert result.path == legacy_shard_dir
    assert result.is_queryable is True


def test_resolve_in_repo_legacy_is_not_queryable_without_hnsw_index(tmp_path):
    """Crash-window case: rows committed, no hnsw_index.bin -- resolved as
    IN_REPO_LEGACY but is_queryable False (the row-existence-not-queryability
    principle, round-10/round-11)."""
    resolver = _make_resolver(tmp_path, repo_alias="evolution")
    legacy_shard_dir = (
        tmp_path
        / "clone"
        / ".code-indexer"
        / "index"
        / "code-indexer-temporal-voyage_code_3-2024Q1"
    )
    nested = legacy_shard_dir / "a"
    nested.mkdir(parents=True)
    (nested / "vector_abc123.json").write_text('{"point_id": "p1"}')

    result = resolver.resolve("voyage_code_3", "2024Q1")

    assert result is not None
    assert result.source == TemporalShardSource.IN_REPO_LEGACY
    assert result.is_queryable is False


def test_catalog_returns_quarters_from_sister_pointers(tmp_path):
    """catalog() enumerates the finite, authoritative set of quarters that
    actually exist for an embedder -- the durable catalog AC8's open-ended
    date-range discovery consults (date math alone cannot produce this)."""
    resolver = _make_resolver(tmp_path, repo_alias="evolution")
    for quarter in ("2024Q1", "2024Q2"):
        v_dir = (
            tmp_path
            / "sister"
            / ".versioned"
            / f"evolution-temporal-voyage_code_3-{quarter}"
            / "v_1"
        )
        resolver._alias_manager.create_alias(
            f"evolution-temporal-voyage_code_3-{quarter}", str(v_dir)
        )
    # A different embedder's pointer must NOT leak into this catalog.
    other_v_dir = (
        tmp_path
        / "sister"
        / ".versioned"
        / "evolution-temporal-embed_v4_0-2024Q1"
        / "v_1"
    )
    resolver._alias_manager.create_alias(
        "evolution-temporal-embed_v4_0-2024Q1", str(other_v_dir)
    )

    result = resolver.catalog("voyage_code_3")

    assert set(result) == {"2024Q1", "2024Q2"}


def test_parse_physical_temporal_name_extracts_slug_and_quarter():
    result = parse_physical_temporal_name("code-indexer-temporal-voyage_code_3-2024Q1")
    assert result == ("voyage_code_3", "2024Q1")


def test_parse_physical_temporal_name_extracts_quarterless_monolith():
    result = parse_physical_temporal_name("code-indexer-temporal-voyage_code_3")
    assert result == ("voyage_code_3", None)


def test_parse_physical_temporal_name_returns_none_for_non_temporal_name():
    result = parse_physical_temporal_name("some-other-collection")
    assert result is None


def test_catalog_unions_in_repo_unbootstrapped_quarters(tmp_path):
    """Per-quarter union resolution (round-8 N1): Q1 already bootstrapped
    (pointer exists) and Q2 not yet bootstrapped (in-repo only, no pointer)
    -- catalog() must include BOTH, with no double-count and no gap."""
    resolver = _make_resolver(tmp_path, repo_alias="evolution")
    v_dir = (
        tmp_path
        / "sister"
        / ".versioned"
        / "evolution-temporal-voyage_code_3-2024Q1"
        / "v_1"
    )
    resolver._alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(v_dir)
    )
    # Q2: in-repo only, real rows, no pointer yet.
    q2_dir = (
        tmp_path
        / "clone"
        / ".code-indexer"
        / "index"
        / "code-indexer-temporal-voyage_code_3-2024Q2"
    )
    (q2_dir / "a").mkdir(parents=True)
    (q2_dir / "a" / "vector_x.json").write_text("{}")

    result = resolver.catalog("voyage_code_3")

    assert set(result) == {"2024Q1", "2024Q2"}
