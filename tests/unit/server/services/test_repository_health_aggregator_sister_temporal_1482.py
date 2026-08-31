"""GitHub Issue #1482 (extension): repository_health_aggregator.py must
include sister-relocated temporal shards (Story #1457 AC1) in health
discovery/aggregation -- it previously scanned ONLY the local clone's
`.code-indexer/index/` directory, so a temporal collection whose data has
relocated to the golden-owned sister location silently vanished from
health reports (neither healthy nor unhealthy -- just absent).

Fix routes through the SAME resolver-aware helper Story #1457/#1459
established (`TemporalShardResolver.catalog()`/`.resolve()`,
`enumerate_candidate_embedder_slugs` from `temporal_status.py`) -- never a
parallel sister-root scan.

Real infra throughout: real AliasManager, a real on-disk sister-location
layout (alias pointer + versioned dir + hnsw_index.bin marker file), real
HNSWHealthService health check (against the fake-but-present hnsw file --
this test only asserts DISCOVERY/presence in the aggregated result, not a
specific valid=True/False outcome, matching the established boundary of
not faking real hnswlib internals).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)
from code_indexer.server.services.repository_health_aggregator import (
    compute_repository_health,
    discover_sister_temporal_collections,
    get_shared_health_service,
)

REPO_ALIAS = "myrepo"
POINTER_NAMESPACE = "myrepo-temporal-voyage_code_3-2024Q1"
PHYSICAL_NAME = "code-indexer-temporal-voyage_code_3-2024Q1"
# Arbitrary but fixed publish-timestamp suffix for the versioned snapshot
# directory this fixture builds -- named to make the on-disk layout intent
# explicit rather than an unexplained magic literal.
FIXTURE_VERSION_TIMESTAMP = "1785164318"


@dataclass
class _SisterFixture:
    legacy_index_path: Path
    golden_repos_dir: Path
    version_dir: Path


def _build_sister_only_repo(tmp_path: Path) -> _SisterFixture:
    golden_repos_dir = tmp_path / "golden-repos"
    legacy_index_path = tmp_path / "clone" / ".code-indexer" / "index"
    legacy_index_path.mkdir(parents=True, exist_ok=True)

    # Bug #1529: the shard lives at the FIXED server-owned root, not a
    # .versioned snapshot behind an alias pointer. The behavior under test is
    # unchanged: health discovery must find temporal data OUTSIDE the repo tree.
    version_dir = (
        server_temporal_index_root(golden_repos_dir, REPO_ALIAS)
        / "code-indexer-temporal-voyage_code_3-2024Q1"
    )
    version_dir.mkdir(parents=True)
    (version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    (version_dir / "collection_meta.json").write_text(json.dumps({"vector_count": 1}))

    return _SisterFixture(
        legacy_index_path=legacy_index_path,
        golden_repos_dir=golden_repos_dir,
        version_dir=version_dir,
    )


class TestDiscoverSisterTemporalCollections:
    def test_finds_sister_only_queryable_shard(self, tmp_path):
        fixture = _build_sister_only_repo(tmp_path)

        discovered = discover_sister_temporal_collections(
            fixture.golden_repos_dir, REPO_ALIAS, fixture.legacy_index_path
        )

        names = [name for name, _index_type, _path in discovered]
        assert PHYSICAL_NAME in names
        for name, index_type, hnsw_path in discovered:
            if name == PHYSICAL_NAME:
                assert index_type == "temporal"
                assert (
                    hnsw_path.resolve()
                    == (fixture.version_dir / "hnsw_index.bin").resolve()
                )

    def test_returns_empty_when_no_sister_data(self, tmp_path):
        golden_repos_dir = tmp_path / "golden-repos"
        legacy_index_path = tmp_path / "clone" / ".code-indexer" / "index"
        legacy_index_path.mkdir(parents=True, exist_ok=True)

        discovered = discover_sister_temporal_collections(
            golden_repos_dir, REPO_ALIAS, legacy_index_path
        )

        assert discovered == []


class TestComputeRepositoryHealthIncludesSisterTemporal:
    def test_sister_relocated_temporal_collection_appears_in_health_result(
        self, tmp_path
    ):
        fixture = _build_sister_only_repo(tmp_path)

        result = compute_repository_health(
            REPO_ALIAS,
            fixture.legacy_index_path,
            get_shared_health_service(),
            golden_repos_dir=fixture.golden_repos_dir,
            golden_repo_alias=REPO_ALIAS,
        )

        collection_names = [c.collection_name for c in result.collections]
        assert PHYSICAL_NAME in collection_names, (
            "compute_repository_health must include sister-relocated "
            f"temporal collections (Bug #1482 extension); got: "
            f"{collection_names!r}"
        )

    def test_no_golden_repos_dir_means_local_only_byte_identical_to_today(
        self, tmp_path
    ):
        """Without golden_repos_dir/golden_repo_alias, behavior must stay
        exactly as before this fix -- local-clone-only discovery."""
        fixture = _build_sister_only_repo(tmp_path)

        result = compute_repository_health(
            REPO_ALIAS,
            fixture.legacy_index_path,
            get_shared_health_service(),
        )

        assert result.collections == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
