"""Tests for GoldenRepoManager._index_exists() temporal branch (Bug #647).

The temporal stub always returned False, causing AI agents to believe
temporal indexes needed rebuilding and triggering destructive --clear wipes.

These tests verify that _index_exists() correctly detects temporal collections
on disk using the real filesystem — no mocks for the detection logic.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock


def _make_manager_for_path(repo_path: str):
    """Create a minimal GoldenRepoManager that resolves alias to repo_path."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    manager = GoldenRepoManager.__new__(GoldenRepoManager)
    manager._metadata_repo = MagicMock()
    # Stub get_actual_repo_path so it returns our controlled temp path
    manager.get_actual_repo_path = MagicMock(return_value=repo_path)
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


def _add_json_file(coll_dir: Path, filename: str = "chunk_0001.json") -> Path:
    """Add a JSON file inside a collection directory."""
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
