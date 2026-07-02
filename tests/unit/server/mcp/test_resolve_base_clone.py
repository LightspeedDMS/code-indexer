"""Tests for _resolve_golden_repo_base_clone helper (Bug #625, Fix 1).

Verifies that the helper correctly extracts the mutable base clone path
from a versioned snapshot alias, returning None when the base clone
does not exist on disk, and returning the path as-is for non-versioned
(legacy flat) structures.
"""

from pathlib import Path
from unittest.mock import patch


class TestResolveGoldenRepoBaseClone:
    """_resolve_golden_repo_base_clone must return the mutable base clone path."""

    def _make_golden_repos_structure(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create golden-repos/{alias}/ base clone + .versioned/{alias}/v_ts/ snapshot."""
        golden_repos_dir = tmp_path / "golden-repos"

        base_clone = golden_repos_dir / "my-repo"
        base_clone.mkdir(parents=True)
        (base_clone / ".code-indexer").mkdir()
        (base_clone / ".code-indexer" / "config.json").write_text(
            '{"embedding_provider":"voyage-ai"}'
        )

        versioned = golden_repos_dir / ".versioned" / "my-repo" / "v_1772136021"
        versioned.mkdir(parents=True)
        (versioned / ".code-indexer").mkdir()
        (versioned / ".code-indexer" / "config.json").write_text(
            '{"embedding_provider":"voyage-ai"}'
        )

        return base_clone, versioned

    def test_resolve_base_clone_from_versioned_path(self, tmp_path):
        """When alias resolves to a versioned snapshot, base clone path is returned."""
        base_clone, versioned = self._make_golden_repos_structure(tmp_path)

        from code_indexer.server.mcp.handlers import _resolve_golden_repo_base_clone

        with patch(
            "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
            return_value=str(versioned),
        ):
            result = _resolve_golden_repo_base_clone("my-repo")

        assert result is not None
        assert Path(result) == base_clone
        assert ".versioned" not in result

    def test_resolve_base_clone_returns_none_if_not_exists(self, tmp_path):
        """When base clone directory does not exist on disk, None is returned."""
        golden_repos_dir = tmp_path / "golden-repos"
        # Only create versioned dir, not the base clone
        versioned = golden_repos_dir / ".versioned" / "ghost-repo" / "v_1772136021"
        versioned.mkdir(parents=True)

        from code_indexer.server.mcp.handlers import _resolve_golden_repo_base_clone

        with patch(
            "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
            return_value=str(versioned),
        ):
            result = _resolve_golden_repo_base_clone("ghost-repo")

        assert result is None

    def test_resolve_base_clone_returns_path_for_non_versioned(self, tmp_path):
        """When alias resolves to a non-versioned (legacy flat) path, it is returned as-is."""
        flat_path = tmp_path / "golden-repos" / "flat-repo"
        flat_path.mkdir(parents=True)

        from code_indexer.server.mcp.handlers import _resolve_golden_repo_base_clone

        with patch(
            "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
            return_value=str(flat_path),
        ):
            result = _resolve_golden_repo_base_clone("flat-repo")

        assert result == str(flat_path)

    def test_resolve_base_clone_returns_none_when_alias_not_found(self):
        """When alias does not exist in the registry, None is returned."""
        from code_indexer.server.mcp.handlers import _resolve_golden_repo_base_clone

        with patch(
            "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
            return_value=None,
        ):
            result = _resolve_golden_repo_base_clone("nonexistent-repo")

        assert result is None
