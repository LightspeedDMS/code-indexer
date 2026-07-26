"""Dedicated temporal read-side FilesystemVectorStore (Story #1457 AC2).

"Introduce a dedicated temporal vector store instance rooted at the sister
location; do NOT thread a second root through the shared semantic store."
The sister_root is the SAME `golden_repos_dir` `VersionedSnapshotManager`
already uses for semantic versioned snapshots (`versioned_base`,
`server/startup/clone_backend_wiring.py`) -- `.versioned/` already lives
there as a SIBLING of each golden repo's clone, so no new physical root
concept is needed; `aliases_dir = golden_repos_dir / "aliases"` matches the
~15 real production `AliasManager` construction sites verbatim.

NOTE (honest scope disclosure): this module builds ONLY the store
CONSTRUCTION function -- proving the resolver is correctly wired so
`_get_collection_path` resolves published temporal names through it. It
does NOT wire this store into any of the five live front-door query call
sites (`semantic_query_manager.py`, `daemon/service.py`,
`multi_search_service.py`, `cli.py`, `temporal_worker.py`) -- that live
wiring is deliberately deferred pending the resolution-scope pin (AC8 Step
6), which is not yet built. Wiring the query hot path without that
protection would reintroduce the exact mid-read deletion hazard the
story's many correction rounds exist to prevent.

Real filesystem, real `AliasManager` -- no mocking of the code under test.
"""

from __future__ import annotations

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_dedicated_store import (
    build_dedicated_temporal_read_store,
)


def test_build_dedicated_temporal_read_store_wires_resolver_and_sister_root(
    tmp_path,
):
    golden_repos_dir = tmp_path / "golden-repos"
    legacy_index_path = (
        tmp_path / "golden-repos" / "evolution" / ".code-indexer" / "index"
    )
    legacy_index_path.mkdir(parents=True)

    # A prior AC6 publish established a sister pointer for this namespace.
    aliases_dir = golden_repos_dir / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    sister_version_dir = (
        golden_repos_dir
        / ".versioned"
        / "evolution-temporal-voyage_code_3-2024Q1"
        / "v_1700000000"
    )
    alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(sister_version_dir)
    )

    store = build_dedicated_temporal_read_store(
        golden_repos_dir, "evolution", legacy_index_path
    )

    resolved_path = store._get_collection_path(
        "code-indexer-temporal-voyage_code_3-2024Q1"
    )

    assert resolved_path == sister_version_dir


def test_build_dedicated_temporal_read_store_rejects_none_golden_repos_dir(tmp_path):
    with pytest.raises(ValueError):
        build_dedicated_temporal_read_store(None, "evolution", tmp_path / "index")


def test_build_dedicated_temporal_read_store_rejects_empty_repo_alias(tmp_path):
    with pytest.raises(ValueError):
        build_dedicated_temporal_read_store(
            tmp_path / "golden-repos", "", tmp_path / "index"
        )


def test_build_dedicated_temporal_read_store_rejects_none_legacy_index_path(
    tmp_path,
):
    with pytest.raises(ValueError):
        build_dedicated_temporal_read_store(
            tmp_path / "golden-repos", "evolution", None
        )
