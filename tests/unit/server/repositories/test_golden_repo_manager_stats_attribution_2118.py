"""Story #2118 slice 1a: golden-repo registration children are attributed.

The initial `cidx index` runs of add_golden_repo carry
``--stats-attribution`` with the global alias (the clone directory is
``golden_repos_dir/<alias>``), the add job's id and purpose ``index`` (initial
registration never records ``refresh``).

Command capture mirrors test_golden_repo_manager_temporal_pg_env_wiring_1313.py:
only the subprocess runners are replaced.
"""

from __future__ import annotations

import inspect
from typing import Dict, List
from unittest.mock import MagicMock, patch

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services.embedding_stats_attribution import (
    StatsAttribution,
)
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.utils.cluster_node_id import resolve_server_config_node_id

_PREFIX = "--stats-attribution="


def _stamped(command: List[str]) -> StatsAttribution:
    [token] = [t for t in command if t.startswith(_PREFIX)]
    return StatsAttribution.from_json(token[len(_PREFIX) :])


def test_post_clone_workflow_commands_carry_the_add_job_attribution(tmp_path) -> None:
    manager = GoldenRepoManager(data_dir=str(tmp_path))
    clone_path = tmp_path / "example-repo"
    clone_path.mkdir()
    by_phase: Dict[str, List[str]] = {}

    def _fake_popen(*, command, phase_name, **_kw):
        by_phase[phase_name] = command
        return 100

    with (
        patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_fake_popen,
        ),
    ):
        manager._execute_post_clone_workflow(
            clone_path=str(clone_path),
            enable_temporal=True,
            job_id="job-add-1",
        )

    expected = StatsAttribution(
        golden_repo_alias="example-repo-global",
        job_id="job-add-1",
        node_id=resolve_server_config_node_id(get_config_service().get_config()),
        purpose="index",
    )
    assert {_stamped(by_phase["semantic"]), _stamped(by_phase["temporal"])} == {
        expected
    }


def test_add_work_job_id_reaches_the_child_commands(tmp_path) -> None:
    """The real execute_add_golden_repo_work; cloning and the subprocess
    runners are stood in. What happens after indexing is not asserted."""
    manager = GoldenRepoManager(data_dir=str(tmp_path))
    clone_path = tmp_path / "example-repo"
    clone_path.mkdir()
    commands: List[List[str]] = []

    def _fake_popen(*, command, phase_name, **_kw):
        commands.append(command)
        return 100

    with (
        patch.object(manager, "_remove_orphan_clone_for_retry"),
        patch.object(manager, "_clone_repository", return_value=str(clone_path)),
        patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_fake_popen,
        ),
    ):
        try:
            manager.execute_add_golden_repo_work(
                repo_url="local://example-repo",
                alias="example-repo",
                default_branch="main",
                submitter_username="admin",
                job_id="job-add-3",
            )
        except Exception:  # noqa: BLE001 -- only the spawned commands matter
            pass

    stamped = [_stamped(c) for c in commands if any(t.startswith(_PREFIX) for t in c)]
    assert stamped, "no `cidx index` child was spawned"
    assert {s.job_id for s in stamped} == {"job-add-3"}


def test_add_golden_repo_worker_declares_job_id(tmp_path) -> None:
    manager = GoldenRepoManager(data_dir=str(tmp_path))
    manager.background_job_manager = MagicMock()
    manager.background_job_manager.submit_job.return_value = "job-add-2"

    manager.add_golden_repo(
        repo_url="local://example-repo",
        alias="example-repo",
        submitter_username="admin",
        skip_pre_flight_git_validation=True,
    )

    worker = manager.background_job_manager.submit_job.call_args.kwargs["func"]
    assert "job_id" in inspect.signature(worker).parameters, (
        "BackgroundJobManager injects job_id only into a worker that declares it"
    )
