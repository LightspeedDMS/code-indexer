"""Bug #1575 Part C remediation (independent re-review): the MCP
provider-index background jobs (_provider_index_job / semantic,
_provider_temporal_index_job / temporal) spawn `cidx index` subprocesses
through the SHARED `_run_provider_subprocess` helper, which never set
CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV at all -- unlike
`_execute_post_clone_workflow`'s golden-repo Popen calls (already fixed),
this shared convergence point silently inherited the unsafe
enabled-by-default fallback even in postgres/cluster mode.

Mirrors test_provider_index_subprocess_env_sanitization_1325.py's
fixtures/mocking style (same Popen/gather_repo_metrics patch targets),
since that is the established pattern for this call site. Parameterized
over (job function, storage_mode) to cover both jobs without duplicating
the setup/assert blocks.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.utils.config_manager import (
    ClaudeIntegrationConfig,
    ServerConfig,
)
from code_indexer.storage.shared.hnsw_sync_state import (
    CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
)
from tests.utils.env_assertions import assert_env_absent, assert_env_value

_FAKE_POSTGRES_DSN = "postgresql://not-a-real-credential@example.invalid/placeholder"


def _make_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "golden-repos" / "my-repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".code-indexer").mkdir()
    config = {"embedding_provider": "cohere"}
    (repo_dir / ".code-indexer" / "config.json").write_text(json.dumps(config))
    return repo_dir


def _mock_popen_proc():
    mock_proc = MagicMock()
    mock_proc.stdout = iter([])
    mock_proc.stderr.readlines.return_value = []
    mock_proc.returncode = 0
    mock_proc.wait.return_value = None
    mock_proc.poll.return_value = 0
    return mock_proc


def _make_server_config(tmp_path: Path, storage_mode: str) -> ServerConfig:
    kwargs = dict(
        server_dir=str(tmp_path / "cidx-server"),
        storage_mode=storage_mode,
        claude_integration_config=ClaudeIntegrationConfig(cohere_api_key="fake-key"),
    )
    if storage_mode == "postgres":
        kwargs["postgres_dsn"] = _FAKE_POSTGRES_DSN
    return ServerConfig(**kwargs)


def _run_job_and_capture_env(job_module_attr: str, tmp_path: Path, storage_mode: str):
    """Run _provider_index_job or _provider_temporal_index_job (imported by
    attribute name from code_indexer.server.mcp.handlers) and return the
    env dict its Popen call received."""
    import code_indexer.server.mcp.handlers as handlers_module

    job_fn = getattr(handlers_module, job_module_attr)

    repo_dir = _make_repo(tmp_path)
    mock_proc = _mock_popen_proc()
    captured_kwargs: dict = {}

    def fake_popen(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        return mock_proc

    server_config = _make_server_config(tmp_path, storage_mode)

    with (
        patch(
            "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
            side_effect=fake_popen,
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(5, 3),
        ),
        patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
        patch("code_indexer.server.mcp.handlers._post_provider_index_snapshot"),
        # resolve_hnsw_sync_epoch_env_var() performs its OWN independent
        # deferred import of get_config_service from its canonical module
        # path, so the name-imported reference above does not affect it.
        patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_cfg_for_resolver,
    ):
        mock_cfg.return_value.get_config.return_value = server_config
        mock_cfg_for_resolver.return_value.get_config.return_value = server_config

        result = job_fn(repo_path=str(repo_dir), provider_name="cohere", clear=False)

    assert result["success"] is True
    return captured_kwargs["env"]


@pytest.mark.slow
@pytest.mark.parametrize(
    "job_module_attr", ["_provider_index_job", "_provider_temporal_index_job"]
)
@pytest.mark.parametrize(
    "storage_mode, expected_var",
    [
        pytest.param("postgres", "1", id="postgres"),
        pytest.param("sqlite", None, id="sqlite"),
    ],
)
def test_hnsw_epoch_env_matches_storage_mode(
    tmp_path, job_module_attr, storage_mode, expected_var
):
    env = _run_job_and_capture_env(job_module_attr, tmp_path, storage_mode)

    if expected_var is None:
        assert_env_absent(
            env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            msg=(
                f"sqlite/solo mode must never set this var on the "
                f"{job_module_attr} child -- it defaults enabled"
            ),
        )
    else:
        assert_env_value(
            env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            expected_var,
            msg=(
                f"the {job_module_attr} child must be told it is running "
                "in postgres/cluster mode"
            ),
        )
