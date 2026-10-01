"""Server-spawned indexing uses server-managed provider settings, never a
repository-authored override.

A golden repository's ``.code-indexer/config.json`` lives inside the
repository working tree, which a tenant (via the file CRUD front door) or an
upstream committer (via the repository's own history) controls. Server-side
``cidx index`` subprocesses load that file through the normal
``ConfigManager.load()`` path, so any field it sets is otherwise trusted
verbatim by the embedding-provider client that reads it -- including the
provider endpoint URL and daemon-mode selection.

``enforce_server_managed_provider_settings`` is the single seam that resets
those specific fields to fixed, server-managed values on a real, loaded
``Config`` object immediately before a server-spawned ``cidx index`` uses it,
regardless of what the repository's own config.json set. It must NOT reset
unrelated provider settings (model, timeout, retries, ...) that the repo or
the server's own config-seeding overlay legitimately customises.
"""

import json

from code_indexer.config import CohereConfig, ConfigManager, VoyageAIConfig
from code_indexer.server.utils.server_managed_provider_settings import (
    enforce_server_managed_provider_settings,
)


def _write_config(path, overrides: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "codebase_dir": str(path.parent.parent),
        "embedding_provider": "voyage-ai",
    }
    data.update(overrides)
    path.write_text(json.dumps(data))


class TestEnforceServerManagedProviderSettings:
    def test_resets_voyage_ai_endpoint_to_server_default(self, tmp_path) -> None:
        config_path = tmp_path / ".code-indexer" / "config.json"
        _write_config(
            config_path,
            {"voyage_ai": {"api_endpoint": "http://127.0.0.1:9/embeddings"}},
        )
        config = ConfigManager(config_path).load()
        assert config.voyage_ai.api_endpoint == "http://127.0.0.1:9/embeddings"

        enforce_server_managed_provider_settings(config)

        assert config.voyage_ai.api_endpoint == VoyageAIConfig().api_endpoint

    def test_resets_cohere_endpoint_to_server_default(self, tmp_path) -> None:
        config_path = tmp_path / ".code-indexer" / "config.json"
        _write_config(
            config_path,
            {"cohere": {"api_endpoint": "http://127.0.0.1:9/v2/embed"}},
        )
        config = ConfigManager(config_path).load()
        assert config.cohere.api_endpoint == "http://127.0.0.1:9/v2/embed"

        enforce_server_managed_provider_settings(config)

        assert config.cohere.api_endpoint == CohereConfig().api_endpoint

    def test_forces_daemon_disabled_when_repo_config_enabled_it(self, tmp_path) -> None:
        config_path = tmp_path / ".code-indexer" / "config.json"
        _write_config(config_path, {"daemon": {"enabled": True}})
        config = ConfigManager(config_path).load()
        assert config.daemon is not None
        assert config.daemon.enabled is True

        enforce_server_managed_provider_settings(config)

        assert config.daemon.enabled is False

    def test_noop_when_config_has_no_daemon_section(self, tmp_path) -> None:
        config_path = tmp_path / ".code-indexer" / "config.json"
        _write_config(config_path, {})
        config = ConfigManager(config_path).load()
        assert config.daemon is None

        enforce_server_managed_provider_settings(config)

        assert config.daemon is None

    def test_preserves_non_endpoint_provider_settings(self, tmp_path) -> None:
        config_path = tmp_path / ".code-indexer" / "config.json"
        _write_config(
            config_path,
            {
                "voyage_ai": {
                    "api_endpoint": "http://127.0.0.1:9/embeddings",
                    "model": "voyage-large-2",
                    "timeout": 42,
                },
                "cohere": {
                    "api_endpoint": "http://127.0.0.1:9/v2/embed",
                    "model": "embed-v3.0",
                },
            },
        )
        config = ConfigManager(config_path).load()

        enforce_server_managed_provider_settings(config)

        assert config.voyage_ai.model == "voyage-large-2"
        assert config.voyage_ai.timeout == 42
        assert config.cohere.model == "embed-v3.0"
