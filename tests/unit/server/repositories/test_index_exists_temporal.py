"""Tests for GoldenRepoManager._index_exists() temporal branch (Bug #647).

The temporal stub always returned False, causing AI agents to believe
temporal indexes needed rebuilding and triggering destructive --clear wipes.

These tests verify that _index_exists() correctly detects temporal collections
on disk using the real filesystem — no mocks for the detection logic.

GitHub Issue #1482 extends this coverage: the temporal branch previously
checked ONLY the local clone's in-repo legacy index directory, so it
reported False for temporal data that Story #1457's AC1 relocation
trigger has moved to the golden-owned sister location. See
TestIndexExistsTemporalSisterLocation below.
"""

import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

from code_indexer.global_repos.alias_manager import AliasManager


def _make_manager_for_path(repo_path: str, golden_repos_dir: Optional[str] = None):
    """Create a minimal GoldenRepoManager that resolves alias to repo_path.

    golden_repos_dir mirrors the real GoldenRepoManager.__init__'s own
    self.golden_repos_dir attribute (Bug #1482's _index_exists fix reads
    it to reroute the temporal branch through the resolver-aware
    get_temporal_repo_status()) -- defaults to a sibling directory inside
    the SAME repo_path tempdir so tests that don't care about the sister
    location still get a valid, harmless value.
    """
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    manager = GoldenRepoManager.__new__(GoldenRepoManager)
    manager._metadata_repo = MagicMock()
    # Stub get_actual_repo_path so it returns our controlled temp path
    manager.get_actual_repo_path = MagicMock(return_value=repo_path)
    manager.golden_repos_dir = golden_repos_dir or str(
        Path(repo_path) / "golden-repos-fixture"
    )
    return manager


def _make_golden_repo(alias: str = "test-repo"):
    """Return a minimal GoldenRepo-like object with just alias."""
    repo = MagicMock()
    repo.alias = alias
    return repo


def _create_temporal_collection(index_dir: Path, name: str) -> Path:
    """Create a temporal collection directory under index_dir and return it."""
    coll = index_dir / name
    coll.mkdir(parents=True, exist_ok=True)
    return coll


def _add_json_file(coll_dir: Path, filename: str = "vector_0001.json") -> Path:
    """Add a JSON file inside a collection directory.

    Default filename matches the real production sharded-legacy naming
    convention (``vector_<hash>.json``, see
    ``filesystem_vector_store.py``'s ``vector_{hash_prefix}.json`` writer) --
    Issue #1459 AC1 made ``_index_exists`` check specifically for
    ``vector_*.json`` files rather than a bare ``*.json`` glob, so a fixture
    using an arbitrary filename no longer represents real on-disk data.
    """
    f = coll_dir / filename
    f.write_text('{"data": "x"}')
    return f


class TestIndexExistsTemporal:
    """_index_exists('temporal') must detect temporal collections on disk."""

    def test_temporal_exists_with_content_returns_true(self):
        """Provider-aware collection dir with .json files -> True."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            index_dir = repo_dir / ".code-indexer" / "index"
            coll = _create_temporal_collection(
                index_dir, "code-indexer-temporal-voyage_3"
            )
            _add_json_file(coll)

            manager = _make_manager_for_path(tmp)
            golden_repo = _make_golden_repo()

            result = manager._index_exists(golden_repo, "temporal")

        assert result is True

    def test_temporal_absent_returns_false(self):
        """No temporal collection dirs at all -> False."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            index_dir = repo_dir / ".code-indexer" / "index"
            # Create index dir but only a non-temporal collection
            other = index_dir / "code-indexer-semantic"
            other.mkdir(parents=True, exist_ok=True)
            _add_json_file(other)

            manager = _make_manager_for_path(tmp)
            golden_repo = _make_golden_repo()

            result = manager._index_exists(golden_repo, "temporal")

        assert result is False

    def test_temporal_legacy_name_with_content_returns_true(self):
        """Legacy 'code-indexer-temporal' (no provider suffix) with .json -> True."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            index_dir = repo_dir / ".code-indexer" / "index"
            coll = _create_temporal_collection(index_dir, "code-indexer-temporal")
            _add_json_file(coll)

            manager = _make_manager_for_path(tmp)
            golden_repo = _make_golden_repo()

            result = manager._index_exists(golden_repo, "temporal")

        assert result is True

    def test_temporal_empty_collection_dir_returns_false(self):
        """Temporal collection dir exists but contains no .json files -> False."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            index_dir = repo_dir / ".code-indexer" / "index"
            # Create collection dir but leave it empty
            _create_temporal_collection(index_dir, "code-indexer-temporal-voyage_3")

            manager = _make_manager_for_path(tmp)
            golden_repo = _make_golden_repo()

            result = manager._index_exists(golden_repo, "temporal")

        assert result is False


class TestIndexExistsTemporalSisterLocation:
    """GitHub Issue #1482: _index_exists('temporal') must ALSO detect
    temporal shard data relocated to the golden-owned sister location
    (Story #1457 AC1) -- previously it checked ONLY the in-repo legacy
    index directory, which relocation empties once it succeeds (true on
    every local-disk/solo server, i.e. production)."""

    def test_sister_only_data_returns_true(self):
        """In-repo legacy index dir is BARE (the actual relocation
        symptom); a valid alias pointer + versioned dir with a real
        hnsw_index.bin exists at the sister location -> True."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "clone"
            index_dir = repo_dir / ".code-indexer" / "index"
            index_dir.mkdir(parents=True, exist_ok=True)

            golden_repos_dir = Path(tmp) / "golden-repos"
            pointer_namespace = "test-repo-temporal-voyage_code_3-2024Q1"
            version_dir = (
                golden_repos_dir / ".versioned" / pointer_namespace / "v_1785164318"
            )
            version_dir.mkdir(parents=True)
            (version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
            alias_manager = AliasManager(str(golden_repos_dir / "aliases"))
            alias_manager.create_alias(pointer_namespace, str(version_dir))

            manager = _make_manager_for_path(
                str(repo_dir), golden_repos_dir=str(golden_repos_dir)
            )
            golden_repo = _make_golden_repo(alias="test-repo")

            result = manager._index_exists(golden_repo, "temporal")

        assert result is True, (
            "_index_exists('temporal') must report True for temporal data "
            "relocated to the golden-owned sister location (Bug #1482)"
        )

    def test_sister_and_in_repo_both_absent_returns_false(self):
        """Neither location has data -> False (no false positive from the
        resolver-based check alone)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "clone"
            index_dir = repo_dir / ".code-indexer" / "index"
            index_dir.mkdir(parents=True, exist_ok=True)
            golden_repos_dir = Path(tmp) / "golden-repos"

            manager = _make_manager_for_path(
                str(repo_dir), golden_repos_dir=str(golden_repos_dir)
            )
            golden_repo = _make_golden_repo(alias="test-repo")

            result = manager._index_exists(golden_repo, "temporal")

        assert result is False


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
