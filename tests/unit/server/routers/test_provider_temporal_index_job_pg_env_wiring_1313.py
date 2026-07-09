"""Bug #1313 round-4 (Codex Finding 2): _provider_temporal_index_job never
merged the PG bootstrap env into the provider API-key env.

Root cause: _provider_temporal_index_job (server/mcp/handlers/repos.py)
builds env = _build_provider_api_key_env(provider_name) (VOYAGE_API_KEY /
CO_API_KEY ONLY), then calls _run_provider_subprocess(cmd, actual_path, env,
"temporal", ...), which passes that env straight to run_with_popen_progress.
That env has NO CIDX_TEMPORAL_PG_BOOTSTRAP_DIR, so the child subprocess
silently used the SQLite backend even in cluster/postgres mode -- the same
root cause as the two round-3 sites, a different entry point (MCP/admin
per-provider temporal index rebuild).

Fix: merge build_temporal_child_env(get_config_service().get_config(),
base_env=env) into the provider env in postgres mode (preserving the API
keys), while staying a no-op (env unchanged) in sqlite mode.
get_config_service().get_config() is used (not ServerConfigManager().load_config())
per CLAUDE.md "Config Bootstrap vs Runtime" -- this module already reads
config exclusively via get_config_service().

Co-located with test_temporal_provider_index.py (same directory), which
already tests the same _provider_temporal_index_job function --
tests/unit/server/mcp/handlers/ does not exist as a directory.

Test-safety note: _build_provider_api_key_env starts from os.environ.copy(),
so this suite runs inside patch.dict(os.environ, {}, clear=True) to avoid
ever capturing the real ambient environment (which may contain unrelated
live credentials on a developer machine) into the captured env dict.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_repo(tmp_path: Path, initial_config: dict) -> Path:
    """Create a minimal repo directory with .code-indexer/config.json."""
    repo_dir = tmp_path / "golden-repos" / "my-repo"
    repo_dir.mkdir(parents=True)
    ci_dir = repo_dir / ".code-indexer"
    ci_dir.mkdir()
    (ci_dir / "config.json").write_text(json.dumps(initial_config))
    return repo_dir


def _mock_server_config(
    voyage_key="voyage-test-key",
    storage_mode="sqlite",
    postgres_dsn=None,
    server_dir="/opt/cidx-server",
) -> MagicMock:
    """Bug #895: _resolve_provider_api_key reads the NESTED
    claude_integration_config.voyageai_api_key, not a flat top-level
    attribute -- must set it there for the mock to be observed. Also carries
    the bootstrap fields (storage_mode/postgres_dsn/server_dir) that
    build_temporal_child_env reads directly off the ServerConfig object."""
    cfg = MagicMock()
    cfg.claude_integration_config.voyageai_api_key = voyage_key
    cfg.claude_integration_config.cohere_api_key = None
    cfg.storage_mode = storage_mode
    cfg.postgres_dsn = postgres_dsn
    cfg.server_dir = server_dir
    return cfg


class TestProviderTemporalIndexJobMergesPgBootstrapEnv:
    def test_postgres_mode_env_has_both_bootstrap_dir_and_api_key(self, tmp_path):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        initial_config = {
            "embedding_provider": "voyage-ai",
            "embedding_providers": ["voyage-ai"],
        }
        repo_dir = _make_repo(tmp_path, initial_config)

        from code_indexer.server.mcp.handlers import _provider_temporal_index_job

        captured_envs: list = []

        def fake_run_popen(command, env=None, **kwargs):
            captured_envs.append(env)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=fake_run_popen,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _mock_server_config(
                storage_mode="postgres",
                postgres_dsn="postgresql://user:pass@host/db",
                server_dir="/opt/cidx-server",
            )

            _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
            )

        assert len(captured_envs) == 1
        env = captured_envs[0]
        assert env is not None

        api_key_present = env.get("VOYAGE_API_KEY") == "voyage-test-key"
        assert api_key_present, (
            "provider API key must be PRESERVED after merging the bootstrap "
            "env, not dropped"
        )

        bootstrap_dir_matches = (
            env.get(TEMPORAL_PG_BOOTSTRAP_DIR_ENV) == "/opt/cidx-server"
        )
        assert bootstrap_dir_matches

    def test_sqlite_mode_env_unchanged_api_key_only(self, tmp_path):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        initial_config = {
            "embedding_provider": "voyage-ai",
            "embedding_providers": ["voyage-ai"],
        }
        repo_dir = _make_repo(tmp_path, initial_config)

        from code_indexer.server.mcp.handlers import _provider_temporal_index_job

        captured_envs: list = []

        def fake_run_popen(command, env=None, **kwargs):
            captured_envs.append(env)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=fake_run_popen,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _mock_server_config(
                storage_mode="sqlite",
                postgres_dsn=None,
                server_dir="/opt/cidx-server",
            )

            _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
            )

        assert len(captured_envs) == 1
        env = captured_envs[0]
        assert env is not None

        api_key_present = env.get("VOYAGE_API_KEY") == "voyage-test-key"
        assert api_key_present

        bootstrap_var_absent = TEMPORAL_PG_BOOTSTRAP_DIR_ENV not in env
        assert bootstrap_var_absent, (
            "sqlite/solo mode must be byte-unchanged: no bootstrap var "
            "must appear in the provider env"
        )
