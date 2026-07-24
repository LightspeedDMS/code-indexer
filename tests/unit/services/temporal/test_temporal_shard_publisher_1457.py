"""Create-if-absent-else-swap publication (Story #1457 AC6 Fix 1).

`AliasManager.swap_alias` CANNOT be used to publish a brand-new per-quarter
namespace: it raises `RuntimeError` when the pointer file is absent
(alias_manager.py) -- neither is satisfiable on a first publish (there is
NO registration-time creation of temporal per-quarter aliases; a brand-new
namespace is born lazily on first index). `publish_temporal_shard_version`
implements the required branch: `create_alias` when the pointer does not
yet exist, `swap_alias` (compare-and-swap against the CURRENT target) when
it does.

Real `AliasManager` against a real tmp_path directory -- no mocking.
"""

from __future__ import annotations

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_shard_publisher import (
    publish_temporal_shard_version,
)


def test_publish_uses_create_alias_when_pointer_absent(tmp_path):
    alias_manager = AliasManager(str(tmp_path / "aliases"))
    new_version_path = (
        tmp_path
        / "sister"
        / ".versioned"
        / "evolution-temporal-voyage_code_3-2024Q1"
        / "v_1700000000"
    )

    publish_temporal_shard_version(
        alias_manager,
        "evolution-temporal-voyage_code_3-2024Q1",
        new_version_path,
    )

    assert alias_manager.read_alias("evolution-temporal-voyage_code_3-2024Q1") == str(
        new_version_path
    )


def test_publish_uses_swap_alias_when_pointer_already_exists(tmp_path):
    alias_manager = AliasManager(str(tmp_path / "aliases"))
    ns = "evolution-temporal-voyage_code_3-2024Q1"
    old_version_path = tmp_path / "sister" / ".versioned" / ns / "v_1700000000"
    new_version_path = tmp_path / "sister" / ".versioned" / ns / "v_1700000100"

    # First publish establishes the pointer (via create_alias).
    publish_temporal_shard_version(alias_manager, ns, old_version_path)

    # Second publish for the SAME namespace must swap, not overwrite blindly.
    publish_temporal_shard_version(alias_manager, ns, new_version_path)

    assert alias_manager.read_alias(ns) == str(new_version_path)
    assert alias_manager.get_previous_path(ns) == str(old_version_path)
