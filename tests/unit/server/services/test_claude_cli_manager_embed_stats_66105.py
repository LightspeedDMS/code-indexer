"""EVO-66105: ClaudeCliManager._commit_and_reindex threads job_id and
sets golden_repo_alias='cidx-meta' when building the child subprocess
env for the meta-repo reindex.

This test verifies that the subprocess env dict passed to the
'cidx index' call contains both CIDX_EMBEDDING_STATS_JOB_ID (from the
caller's job_id) and CIDX_EMBEDDING_STATS_GOLDEN_REPO_ALIAS (hardcoded
to 'cidx-meta').
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from code_indexer.server.services.claude_cli_manager import ClaudeCliManager
from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
    EMBEDDING_STATS_GOLDEN_REPO_ALIAS_ENV,
    EMBEDDING_STATS_JOB_ID_ENV,
)
from code_indexer.server.utils.config_manager import ServerConfig


class TestCommitAndReindexEmbedStats:
    def test_cidx_index_receives_embed_stats_env_vars(self, tmp_path):
        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()

        manager = ClaudeCliManager(api_key=None, max_workers=0)
        manager.set_meta_dir(meta_dir)

        server_config = ServerConfig(
            server_dir="/opt/cidx-server", storage_mode="sqlite"
        )

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
                "code_indexer.server.services.claude_cli_manager.get_config_service"
            ) as mock_cfg,
        ):
            mock_cfg.return_value.get_config.return_value = server_config
            manager._commit_and_reindex(
                ["some-alias"],
                job_id="meta-job-789",
            )

        # Upstream appends server-layout flags via append_server_layout_args(),
        # so match the verb prefix rather than the exact argv (EVO-66105 asserts
        # the embed-stats env, not the command's flag set).
        index_calls = [c for c in run_calls if c["cmd"][:2] == ["cidx", "index"]]
        assert index_calls, f"expected a 'cidx index' call, got: {run_calls}"
        index_env = index_calls[0]["kwargs"].get("env")
        assert index_env is not None, "cidx index must receive a sanitized env"
        assert index_env.get(EMBEDDING_STATS_GOLDEN_REPO_ALIAS_ENV) == "cidx-meta"
        assert index_env.get(EMBEDDING_STATS_JOB_ID_ENV) == "meta-job-789"
