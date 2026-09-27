"""Indexer resume-state trust and containment.

Server-spawned indexing (golden-repo add/refresh) must
not trust repository-authored resume state
(.code-indexer/metadata-<provider>.json lives inside the cloned repo's
working tree -- writable by whoever controls the repo's commit history).
The chosen mechanism is the new `cidx index --ignore-resume-state` CLI
flag (threads SmartIndexer.smart_index(trust_resume_state=False)) rather
than `--clear` (which would force a full re-embed on every server-spawned
run -- prohibitive at ~900-repo production scale).

These tests exercise the REAL `_execute_post_clone_workflow` (used for
BOTH golden-repo add and incremental refresh -- see its own force_init
docstring) and `_cb_cidx_index` (golden-repo branch-change reindex),
capturing the actual subprocess/Popen command argv via the same
mocking pattern as
tests/unit/server/repositories/test_golden_repo_manager_subprocess_env_sanitization_1325.py,
and assert the semantic+FTS command includes --ignore-resume-state.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.utils.config_manager import ServerConfig


def _capture_popen(calls):
    def _fake(*, command, phase_name, env=None, **kwargs):
        calls.append({"command": command, "phase_name": phase_name, "env": env})
        return 100

    return _fake


def test_post_clone_workflow_semantic_fts_command_ignores_resume_state(
    tmp_path,
) -> None:
    """_execute_post_clone_workflow (golden-repo add AND refresh) must spawn
    `cidx index --fts --progress-json --ignore-resume-state`."""
    manager = GoldenRepoManager(data_dir=str(tmp_path))
    clone_path = tmp_path / "test-repo"
    clone_path.mkdir()
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")

    popen_calls: list = []
    with (
        patch("subprocess.run", return_value=Mock(returncode=0, stdout="", stderr="")),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_capture_popen(popen_calls),
        ),
        patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_get_cfg_svc,
    ):
        mock_get_cfg_svc.return_value.get_config.return_value = server_config
        manager._execute_post_clone_workflow(
            clone_path=str(clone_path),
            force_init=False,
            enable_temporal=False,
            temporal_options=None,
        )

    by_phase = {c["phase_name"]: c["command"] for c in popen_calls}
    assert "semantic" in by_phase, f"expected a semantic-phase call, got: {popen_calls}"
    semantic_command = by_phase["semantic"]
    assert "--ignore-resume-state" in semantic_command, (
        "SECURITY: golden-repo add/refresh's semantic+FTS `cidx index` "
        "command must never trust committer-authored resume state. "
        f"Got command: {semantic_command}"
    )


def test_cb_cidx_index_branch_change_command_ignores_resume_state(tmp_path) -> None:
    """_cb_cidx_index (golden-repo branch-change reindex) must spawn
    `cidx index --fts --ignore-resume-state`."""
    with patch.object(GoldenRepoManager, "__init__", lambda self, *a, **kw: None):
        manager = GoldenRepoManager.__new__(GoldenRepoManager)

    base_clone_path = tmp_path / "base-clone"
    base_clone_path.mkdir()

    captured: dict = {}

    def _capture_run(command, **kwargs):
        captured["command"] = command
        return Mock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=_capture_run):
        manager._cb_cidx_index(str(base_clone_path))

    assert "command" in captured, "expected subprocess.run to be called"
    assert "--ignore-resume-state" in captured["command"], (
        "SECURITY: golden-repo branch-change's `cidx index` command must "
        f"never trust committer-authored resume state. Got: {captured['command']}"
    )
