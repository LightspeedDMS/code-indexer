"""Bug #1325: cidx subprocesses spawned by GoldenRepoManager with cwd=<clone>
must never inherit a RELATIVE PYTHONPATH unchanged from the server process.

Root cause: when the server is launched via the documented dev command
(`PYTHONPATH=./src python3 -m uvicorn code_indexer.server.app:app`), that
relative PYTHONPATH entry is inherited unchanged by `cidx init`/`cidx index`
child subprocesses. Because PYTHONPATH resolution is relative to the
CURRENT process's cwd, and these children run with cwd=<clone_path>, the
relative entry re-anchors into the clone directory. If the clone has its own
`src/`-layout package colliding with a real cidx dependency (e.g. `click`),
the clone's package shadows the installed dependency and cidx's own imports
break -- causing `cidx init failed` / `cidx index failed` and hard failure
of golden-repo registration.

Fix: every cidx subprocess call site in golden_repo_manager.py with
cwd=<clone_or_repo_path> passes env=build_cidx_subprocess_env(...), which
absolutizes any relative PYTHONPATH entry before the child changes cwd.

These are call-site wiring tests proving the fix for BOTH launch paths that
spawn an init subprocess.run call plus a semantic Popen call:
- _execute_post_clone_workflow (golden repo registration)
- add_indexes_to_golden_repo (add-index background worker)

Note: in the post-clone-workflow path, semantic + FTS are combined into ONE
Popen call (`cidx index --fts --progress-json`, phase_name="semantic"). In
the add-indexes path, FTS runs as its own direct subprocess.run call (not
Popen) -- only the init and semantic phases go through the Popen-style
runner in that path, which is what these tests target.
"""

from __future__ import annotations

import os
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.utils.config_manager import ServerConfig

_RELATIVE_PYTHONPATH = "./src"


def _make_add_indexes_manager(tmp_path, temporal_options=None):
    """Build a minimal GoldenRepoManager with a real repo_path on disk."""
    with patch.object(GoldenRepoManager, "__init__", lambda self, *a, **kw: None):
        manager = GoldenRepoManager.__new__(GoldenRepoManager)

    repo_path = tmp_path / "golden-repos" / "test-repo"
    (repo_path / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

    manager.data_dir = str(tmp_path)
    manager.golden_repos_dir = str(tmp_path / "golden-repos")

    golden_repo = Mock()
    golden_repo.alias = "test-repo"
    golden_repo.clone_path = str(repo_path)
    golden_repo.temporal_options = temporal_options or {}
    golden_repo.enable_temporal = False

    manager.golden_repos = {"test-repo": golden_repo}
    manager.get_actual_repo_path = Mock(return_value=str(repo_path))
    manager._sqlite_backend = Mock()
    manager._sqlite_backend.update_enable_temporal = Mock(return_value=True)
    # Bug #1316: add_indexes_to_golden_repo's background_worker now resolves
    # `repo` via _resolve_golden_repo_authoritative, which unconditionally
    # calls _sqlite_backend.get_repo(alias) -- configure it to mirror the
    # golden_repo Mock above so GoldenRepo(**repo_data) succeeds.
    manager._sqlite_backend.get_repo = Mock(
        return_value={
            "alias": golden_repo.alias,
            "repo_url": "https://example.com/test-repo.git",
            "default_branch": "main",
            "clone_path": golden_repo.clone_path,
            "created_at": "2026-01-01T00:00:00+00:00",
            "enable_temporal": golden_repo.enable_temporal,
            "temporal_options": golden_repo.temporal_options,
            "category_id": None,
            "category_auto_assigned": False,
        }
    )
    manager._global_repos_backend = Mock()

    captured_workers = []

    def capture_and_run(operation_type, func, submitter_username, **kwargs):
        captured_workers.append(func)
        return "job-add-indexes-test"

    manager.background_job_manager = Mock()
    manager.background_job_manager.submit_job.side_effect = capture_and_run
    manager._captured_workers = captured_workers
    manager._refresh_scheduler = None

    return manager


def _run_captured_worker(manager) -> None:
    assert len(manager._captured_workers) == 1
    manager._captured_workers[0]()


def _capture_run(calls):
    def _run(command, **kwargs):
        calls.append(kwargs)
        return Mock(returncode=0, stdout="", stderr="")

    return _run


def _capture_popen(calls):
    def _fake(*, command, phase_name, env=None, **kwargs):
        calls.append({"phase_name": phase_name, "env": env})
        return 100

    return _fake


def _run_post_clone_scenario(tmp_path):
    """Exercise _execute_post_clone_workflow, capturing subprocess.run/Popen calls."""
    manager = GoldenRepoManager(data_dir=str(tmp_path))
    clone_path = tmp_path / "test-repo"
    clone_path.mkdir()
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")

    run_calls: list = []
    popen_calls: list = []
    with (
        patch("subprocess.run", side_effect=_capture_run(run_calls)),
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
    return run_calls, popen_calls


def _run_add_indexes_scenario(tmp_path):
    """Exercise add_indexes_to_golden_repo, capturing subprocess.run/Popen calls."""
    manager = _make_add_indexes_manager(tmp_path)
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")

    run_calls: list = []
    popen_calls: list = []
    with (
        patch(
            "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
            side_effect=_capture_run(run_calls),
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_capture_popen(popen_calls),
        ),
        patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_get_cfg_svc,
    ):
        mock_get_cfg_svc.return_value.get_config.return_value = server_config
        manager.add_indexes_to_golden_repo(alias="test-repo", index_types=["semantic"])
        _run_captured_worker(manager)
    return run_calls, popen_calls


_SCENARIOS = {
    "post_clone_workflow": _run_post_clone_scenario,
    "add_indexes_to_golden_repo": _run_add_indexes_scenario,
}


@pytest.mark.parametrize("scenario_name", list(_SCENARIOS.keys()))
def test_init_subprocess_receives_absolutized_pythonpath(
    scenario_name, monkeypatch, tmp_path
):
    """The cidx init subprocess.run call must receive an absolutized PYTHONPATH."""
    monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
    expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

    run_calls, _popen_calls = _SCENARIOS[scenario_name](tmp_path)

    assert len(run_calls) >= 1, "expected at least one subprocess.run call (init)"
    init_env = run_calls[0].get("env")
    assert init_env is not None, "init subprocess.run must receive a sanitized env"
    assert init_env["PYTHONPATH"] == expected_abs


@pytest.mark.parametrize("scenario_name", list(_SCENARIOS.keys()))
def test_semantic_popen_call_receives_absolutized_pythonpath(
    scenario_name, monkeypatch, tmp_path
):
    """The semantic (FTS-combined, in post-clone) Popen call must receive an absolutized PYTHONPATH."""
    monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
    expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

    _run_calls, popen_calls = _SCENARIOS[scenario_name](tmp_path)

    by_phase = {c["phase_name"]: c["env"] for c in popen_calls}
    assert "semantic" in by_phase, f"expected a semantic-phase call, got: {popen_calls}"
    semantic_env = by_phase["semantic"]
    assert semantic_env is not None, "semantic Popen call must receive a sanitized env"
    assert semantic_env["PYTHONPATH"] == expected_abs
