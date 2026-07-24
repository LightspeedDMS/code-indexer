"""resolve_overlapping_shards() -- resolver-aware discovery (Story #1457
AC8 dispatch consumption contract items 1-2).

Discovery-level resolver wiring: given a repo's TemporalShardResolver and
an embedder slug, returns the List[ResolvedTemporalShard] whose date range
overlaps [start, end] -- reusing TemporalShardResolver.catalog() (the
authoritative pointer+in-repo union, already built and tested for AC8) and
.resolve() (pointer-first resolution), NEVER a bare index_path.iterdir()
scan. This is what finally lets `.is_queryable` be filtered BEFORE any
shard is queried (item 2) and lets resolved objects be retained through the
whole query loop (item 1) -- as opposed to the existing byte-identical
get_overlapping_shards(), which is left completely untouched for the
CLI/solo and any non-resolver caller.

Real AliasManager, real TemporalShardResolver, real filesystem -- no
mocking of the code under test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
    TemporalShardSource,
    resolve_overlapping_shards,
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


def test_resolve_overlapping_shards_returns_empty_when_nothing_exists(tmp_path):
    resolver = _make_resolver(tmp_path)
    result = resolve_overlapping_shards(resolver, "voyage_code_3", None, None)
    assert result == []


def test_resolve_overlapping_shards_filters_by_date_range_via_sister_pointers(
    tmp_path,
):
    """4 published sister quarters; a [Apr, Sep] range must return only
    Q2+Q3, resolved via the pointer (not a bare iterdir scan)."""
    resolver = _make_resolver(tmp_path)
    for suffix in ["2024Q1", "2024Q2", "2024Q3", "2024Q4"]:
        version_dir = (
            resolver._sister_root
            / ".versioned"
            / f"evolution-temporal-voyage_code_3-{suffix}"
            / "v_1700000000"
        )
        version_dir.mkdir(parents=True)
        resolver._alias_manager.create_alias(
            f"evolution-temporal-voyage_code_3-{suffix}", str(version_dir)
        )

    start = datetime(2024, 4, 1, tzinfo=timezone.utc)
    end = datetime(2024, 9, 30, tzinfo=timezone.utc)
    result = resolve_overlapping_shards(resolver, "voyage_code_3", start, end)

    quarters = {r.pointer_namespace for r in result}
    assert "evolution-temporal-voyage_code_3-2024Q2" in quarters
    assert "evolution-temporal-voyage_code_3-2024Q3" in quarters
    assert "evolution-temporal-voyage_code_3-2024Q1" not in quarters
    assert "evolution-temporal-voyage_code_3-2024Q4" not in quarters
    for r in result:
        assert r.source == TemporalShardSource.SISTER_POINTER
        assert r.is_queryable is True


def test_resolve_overlapping_shards_all_time_returns_all_quarters(tmp_path):
    resolver = _make_resolver(tmp_path)
    for suffix in ["2024Q1", "2024Q2", "2024Q3"]:
        version_dir = (
            resolver._sister_root
            / ".versioned"
            / f"evolution-temporal-voyage_code_3-{suffix}"
            / "v_1700000000"
        )
        version_dir.mkdir(parents=True)
        resolver._alias_manager.create_alias(
            f"evolution-temporal-voyage_code_3-{suffix}", str(version_dir)
        )

    result = resolve_overlapping_shards(resolver, "voyage_code_3", None, None)

    assert len(result) == 3


def test_resolve_overlapping_shards_includes_monolith_when_present(tmp_path):
    """A quarter-less monolith namespace (quarter=None) is always included
    regardless of date range, appended last (catalog()'s established
    None-last ordering, unchanged)."""
    resolver = _make_resolver(tmp_path)
    version_dir = (
        resolver._sister_root
        / ".versioned"
        / "evolution-temporal-voyage_code_3"
        / "v_1700000000"
    )
    version_dir.mkdir(parents=True)
    resolver._alias_manager.create_alias(
        "evolution-temporal-voyage_code_3", str(version_dir)
    )

    result = resolve_overlapping_shards(resolver, "voyage_code_3", None, None)

    assert len(result) == 1
    assert result[0].pointer_namespace == "evolution-temporal-voyage_code_3"


def test_resolve_overlapping_shards_prefers_sister_over_in_repo_legacy(tmp_path):
    """A quarter with BOTH a sister pointer and real in-repo legacy rows
    resolves via the pointer -- the union-catalog + pointer-first rule
    already proven for resolve()/catalog(), now exercised end-to-end
    through discovery."""
    resolver = _make_resolver(tmp_path)

    legacy_shard = (
        resolver._legacy_index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    )
    legacy_shard.mkdir(parents=True)
    (legacy_shard / "vector_abc.json").write_text('{"id": "p1"}')

    version_dir = (
        resolver._sister_root
        / ".versioned"
        / "evolution-temporal-voyage_code_3-2024Q1"
        / "v_1700000000"
    )
    version_dir.mkdir(parents=True)
    resolver._alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(version_dir)
    )

    result = resolve_overlapping_shards(resolver, "voyage_code_3", None, None)

    assert len(result) == 1
    assert result[0].source == TemporalShardSource.SISTER_POINTER
    assert result[0].path == version_dir
