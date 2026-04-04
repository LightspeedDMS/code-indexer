"""Tests for _remove_provider_from_config and defensive guard in _append_provider_to_config (Bug #625, Fix 5/6).

Verifies:
1. _remove_provider_from_config removes cohere from embedding_providers
2. _remove_provider_from_config is idempotent (safe to call multiple times)
3. _remove_provider_from_config protects voyage-ai (primary provider cannot be removed)
4. _append_provider_to_config logs a warning when given a versioned snapshot path
5. _remove_provider_from_config handles missing config.json silently (defensive edge case)
"""

import json
import logging
from pathlib import Path


class TestRemoveProviderFromConfig:
    """_remove_provider_from_config must update config.json correctly."""

    def _make_config(self, tmp_path: Path, providers: list) -> Path:
        """Create a minimal .code-indexer/config.json with the given embedding_providers."""
        config_dir = tmp_path / ".code-indexer"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"
        config_path.write_text(
            json.dumps(
                {"embedding_provider": "voyage-ai", "embedding_providers": providers}
            )
        )
        return tmp_path

    def test_remove_provider_from_config_removes_cohere(self, tmp_path):
        """cohere is removed from embedding_providers list."""
        repo_path = self._make_config(tmp_path, ["voyage-ai", "cohere"])

        from code_indexer.server.mcp.handlers import _remove_provider_from_config

        _remove_provider_from_config(str(repo_path), "cohere")

        config = json.loads((repo_path / ".code-indexer" / "config.json").read_text())
        assert "cohere" not in config["embedding_providers"]
        assert "voyage-ai" in config["embedding_providers"]

    def test_remove_provider_from_config_idempotent(self, tmp_path):
        """Calling remove twice for the same provider does not raise and leaves config valid."""
        repo_path = self._make_config(tmp_path, ["voyage-ai", "cohere"])

        from code_indexer.server.mcp.handlers import _remove_provider_from_config

        _remove_provider_from_config(str(repo_path), "cohere")
        # Second call must not raise
        _remove_provider_from_config(str(repo_path), "cohere")

        config = json.loads((repo_path / ".code-indexer" / "config.json").read_text())
        assert "cohere" not in config["embedding_providers"]

    def test_remove_provider_from_config_protects_voyage_ai(self, tmp_path, caplog):
        """voyage-ai cannot be removed — a warning is logged and config is unchanged."""
        repo_path = self._make_config(tmp_path, ["voyage-ai", "cohere"])

        from code_indexer.server.mcp.handlers import _remove_provider_from_config

        with caplog.at_level(logging.WARNING):
            _remove_provider_from_config(str(repo_path), "voyage-ai")

        config = json.loads((repo_path / ".code-indexer" / "config.json").read_text())
        assert "voyage-ai" in config["embedding_providers"]
        assert any("voyage-ai" in msg or "primary" in msg for msg in caplog.messages)

    def test_remove_provider_from_config_no_config_file(self, tmp_path):
        """When config.json does not exist, function returns silently without error."""
        from code_indexer.server.mcp.handlers import _remove_provider_from_config

        # tmp_path has no .code-indexer/config.json
        _remove_provider_from_config(str(tmp_path), "cohere")
        # No exception raised — test passes


class TestAppendProviderToConfigDefensiveGuard:
    """_append_provider_to_config must refuse (early return) when given a versioned snapshot path."""

    def _make_versioned_config(self, tmp_path: Path) -> Path:
        """Create a versioned snapshot directory structure with config.json."""
        versioned = (
            tmp_path / "golden-repos" / ".versioned" / "my-repo" / "v_1772136021"
        )
        config_dir = versioned / ".code-indexer"
        config_dir.mkdir(parents=True)
        config_dir.joinpath("config.json").write_text(
            json.dumps({"embedding_provider": "voyage-ai"})
        )
        return versioned

    def test_append_refuses_versioned_path_does_not_write(self, tmp_path, caplog):
        """_append_provider_to_config must NOT write to a versioned snapshot path (Anti-Fallback).

        Critical 3 (Bug #625): the guard must be an early return, not a mere warning.
        The config.json inside the versioned snapshot must remain unmodified.
        """
        versioned_path = self._make_versioned_config(tmp_path)
        original_config = json.loads(
            (versioned_path / ".code-indexer" / "config.json").read_text()
        )

        from code_indexer.server.mcp.handlers import _append_provider_to_config

        with caplog.at_level(logging.WARNING):
            _append_provider_to_config(str(versioned_path), "cohere")

        # Config must NOT have been modified — the function must refuse and return early.
        config_after = json.loads(
            (versioned_path / ".code-indexer" / "config.json").read_text()
        )
        assert config_after == original_config, (
            "_append_provider_to_config must not modify config inside .versioned/ "
            f"(Anti-Fallback, Bug #625). Before: {original_config}, After: {config_after}"
        )
        # An error/warning must still be logged.
        assert any(
            ".versioned" in msg or "versioned" in msg.lower() for msg in caplog.messages
        ), f"Expected log message about versioned path, got: {caplog.messages}"

    def test_append_warns_on_versioned_path(self, tmp_path, caplog):
        """A warning/error is emitted when repo_path contains .versioned in its parts."""
        versioned_path = self._make_versioned_config(tmp_path)

        from code_indexer.server.mcp.handlers import _append_provider_to_config

        with caplog.at_level(logging.WARNING):
            _append_provider_to_config(str(versioned_path), "cohere")

        assert any(
            ".versioned" in msg or "versioned" in msg.lower() for msg in caplog.messages
        ), f"Expected warning about versioned path, got: {caplog.messages}"


class TestRemoveProviderFromConfigVersionedGuard:
    """_remove_provider_from_config must refuse versioned snapshot paths (Anti-Fallback, Bug #625)."""

    def _make_versioned_config(self, tmp_path: Path, providers: list) -> Path:
        """Create a versioned snapshot directory structure with config.json."""
        versioned = (
            tmp_path / "golden-repos" / ".versioned" / "my-repo" / "v_1772136021"
        )
        config_dir = versioned / ".code-indexer"
        config_dir.mkdir(parents=True)
        config_dir.joinpath("config.json").write_text(
            json.dumps(
                {"embedding_provider": "voyage-ai", "embedding_providers": providers}
            )
        )
        return versioned

    def test_remove_refuses_versioned_path_does_not_write(self, tmp_path, caplog):
        """_remove_provider_from_config must NOT write to a versioned snapshot path.

        Critical 3 (Bug #625): versioned paths are immutable. The guard must refuse
        and return early rather than modifying the config.json.
        """
        versioned_path = self._make_versioned_config(tmp_path, ["voyage-ai", "cohere"])
        original_config = json.loads(
            (versioned_path / ".code-indexer" / "config.json").read_text()
        )

        from code_indexer.server.mcp.handlers import _remove_provider_from_config

        with caplog.at_level(logging.WARNING):
            _remove_provider_from_config(str(versioned_path), "cohere")

        # Config must NOT have been modified.
        config_after = json.loads(
            (versioned_path / ".code-indexer" / "config.json").read_text()
        )
        assert config_after == original_config, (
            "_remove_provider_from_config must not modify config inside .versioned/ "
            f"(Anti-Fallback, Bug #625). Before: {original_config}, After: {config_after}"
        )
        # An error/warning must be logged.
        assert any(
            ".versioned" in msg or "versioned" in msg.lower() for msg in caplog.messages
        ), f"Expected log message about versioned path, got: {caplog.messages}"
