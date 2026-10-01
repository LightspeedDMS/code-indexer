"""Bug #1575 Part C remediation (independent re-review, bonus site the
reviewer did not enumerate): ClaudeCliManager._commit_and_reindex spawns a
plain `cidx index` subprocess (the cidx-meta / dep-map catch-up reindex)
that never set CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV at all -- unlike
`_execute_post_clone_workflow`'s golden-repo Popen calls (already fixed),
this spawn site silently inherited the unsafe enabled-by-default fallback
even in postgres/cluster mode.

Mirrors test_claude_cli_manager_subprocess_env_sanitization_1325.py's
construction and calling convention exactly.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from code_indexer.server.services.claude_cli_manager import ClaudeCliManager
from code_indexer.server.utils.config_manager import ServerConfig
from code_indexer.storage.shared.hnsw_sync_state import (
    CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
)
from tests.utils.env_assertions import assert_env_absent, assert_env_value

_FAKE_POSTGRES_DSN = "postgresql://not-a-real-credential@example.invalid/placeholder"


def _run_commit_and_reindex_and_capture_index_env(tmp_path, server_config):
    meta_dir = tmp_path / "cidx-meta"
    meta_dir.mkdir()

    manager = ClaudeCliManager(api_key=None, max_workers=0)
    manager.set_meta_dir(meta_dir)

    run_calls: list = []

    def _run(cmd, **kwargs):
        run_calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return Mock(returncode=0, stdout="", stderr="")

    with (
        patch(
            "code_indexer.server.services.claude_cli_manager.subprocess.run",
            side_effect=_run,
        ),
        patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_get_cfg_svc,
    ):
        mock_get_cfg_svc.return_value.get_config.return_value = server_config
        manager._commit_and_reindex(["some-alias"])

    index_calls = [c for c in run_calls if c["cmd"][:2] == ["cidx", "index"]]
    assert index_calls, f"expected a 'cidx index' call, got: {run_calls}"
    return index_calls[0]["kwargs"].get("env")


@pytest.mark.parametrize(
    "storage_mode, expected_var",
    [
        pytest.param("postgres", "1", id="postgres"),
        pytest.param("sqlite", None, id="sqlite"),
    ],
)
def test_hnsw_epoch_env_matches_storage_mode(tmp_path, storage_mode, expected_var):
    kwargs = dict(server_dir=str(tmp_path / "cidx-server"), storage_mode=storage_mode)
    if storage_mode == "postgres":
        kwargs["postgres_dsn"] = _FAKE_POSTGRES_DSN
    server_config = ServerConfig(**kwargs)

    env = _run_commit_and_reindex_and_capture_index_env(tmp_path, server_config)

    assert env is not None
    if expected_var is None:
        assert_env_absent(
            env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            msg="sqlite/solo mode must never set this var -- child defaults enabled",
        )
    else:
        assert_env_value(
            env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            expected_var,
            msg=(
                "the cidx-meta catch-up reindex `cidx index` child must be "
                "told it is running in postgres/cluster mode"
            ),
        )
