"""Tests for GoldenRepoManager._write_embedding_providers_to_config() (Story #620)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_manager():
    """Create a GoldenRepoManager with minimal mocked dependencies."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    manager = GoldenRepoManager.__new__(GoldenRepoManager)
    manager._metadata_repo = MagicMock()
    return manager


def _setup_repo_config(tmp_dir: str, base: dict) -> Path:
    """Create .code-indexer/config.json in tmp_dir and return config file path."""
    config_dir = Path(tmp_dir) / ".code-indexer"
    config_dir.mkdir(exist_ok=True)
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps(base))
    return config_file


def _exercise_write(manager, tmp_dir: str, cohere_key) -> dict:
    """Seed config, patch get_configured_providers, invoke, return parsed config."""
    _setup_repo_config(tmp_dir, {"embedding_provider": "voyage-ai", "sentinel": 42})

    # Build expected provider list based on cohere_key
    providers = ["voyage-ai"]
    if cohere_key:
        providers.append("cohere")

    with patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers",
        return_value=list(providers),
    ):
        manager._write_embedding_providers_to_config(tmp_dir)

    config_file = Path(tmp_dir) / ".code-indexer" / "config.json"
    return json.loads(config_file.read_text())


class TestWriteEmbeddingProvidersToConfig:
    """Test _write_embedding_providers_to_config writes the correct providers list."""

    def test_writes_voyage_and_cohere_when_both_configured(self):
        """Writes both providers (no duplicates) when cohere API key is present."""
        manager = _make_manager()
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = _exercise_write(manager, tmp_dir, cohere_key="cohere-key-123")

        providers = result["embedding_providers"]
        assert set(providers) == {"voyage-ai", "cohere"}
        assert len(providers) == 2

    def test_writes_only_voyage_when_no_cohere_key(self):
        """Writes only voyage-ai when cohere API key is None."""
        manager = _make_manager()
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = _exercise_write(manager, tmp_dir, cohere_key=None)

        assert result["embedding_providers"] == ["voyage-ai"]

    def test_writes_only_voyage_when_cohere_key_is_empty_string(self):
        """Guard: empty string cohere key is treated as not configured."""
        manager = _make_manager()
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = _exercise_write(manager, tmp_dir, cohere_key="")

        assert result["embedding_providers"] == ["voyage-ai"]

    def test_preserves_existing_config_keys(self):
        """Writing embedding_providers preserves all other keys in config.json."""
        manager = _make_manager()
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = _exercise_write(manager, tmp_dir, cohere_key=None)

        assert result["embedding_provider"] == "voyage-ai"
        assert result["sentinel"] == 42
