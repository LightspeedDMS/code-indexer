"""Story #2118 slice 1a: golden-repo refresh children are attributed.

Every `cidx index` command the refresh scheduler spawns carries
``--stats-attribution`` with the global alias, the background job's id (or
``nojob:refresh_scheduler`` outside a job), the node id and purpose
``refresh``. The job id reaches ``_index_source`` from the BackgroundJob
worker (BackgroundJobManager injects ``job_id`` into a worker that declares
it) and from DistributedJobWorkerService's claimed-job path.

Command capture mirrors test_refresh_scheduler_ignore_resume_state.py: only
the subprocess runner is replaced; the command construction runs for real.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Dict, List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.embedding_stats_attribution import (
    StatsAttribution,
)
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.utils.cluster_node_id import resolve_server_config_node_id

_PREFIX = "--stats-attribution="


@pytest.fixture
def scheduler(tmp_path) -> RefreshScheduler:
    golden_repos_dir = tmp_path / "golden_repos"
    golden_repos_dir.mkdir()
    registry = MagicMock()
    registry.get_global_repo.return_value = {
        "alias": "example-repo-global",
        "repo_url": "https://example.com/example-repo.git",
        "enable_temporal": True,
        "temporal_options": None,
        "enable_scip": False,
    }
    query_tracker = QueryTracker()
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=ConfigManager(tmp_path / ".code-indexer" / "config.json"),
        query_tracker=query_tracker,
        cleanup_manager=CleanupManager(query_tracker),
        registry=registry,
        background_job_manager=MagicMock(),
    )


def _attribution_by_phase(scheduler, source, **kwargs) -> Dict[str, StatsAttribution]:
    calls: List[dict] = []

    def _fake(*, command, phase_name, **_kw):
        calls.append({"command": command, "phase_name": phase_name})
        return 100

    with patch(
        "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
        side_effect=_fake,
    ):
        scheduler._index_source(
            alias_name="example-repo-global", source_path=str(source), **kwargs
        )
    found: Dict[str, StatsAttribution] = {}
    for call in calls:
        [token] = [t for t in call["command"] if t.startswith(_PREFIX)]
        found[call["phase_name"]] = StatsAttribution.from_json(token[len(_PREFIX) :])
    return found


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "source_repo"
    (src / ".git").mkdir(parents=True)
    (src / "README.md").write_text("# Example\n")
    return src


def test_index_source_commands_carry_the_job_attribution(scheduler, source) -> None:
    found = _attribution_by_phase(scheduler, source, job_id="job-77")

    expected = StatsAttribution(
        golden_repo_alias="example-repo-global",
        job_id="job-77",
        node_id=resolve_server_config_node_id(get_config_service().get_config()),
        purpose="refresh",
    )
    assert set(found) >= {"semantic", "temporal"}, found
    assert {found["semantic"], found["temporal"]} == {expected}


def test_index_source_outside_a_job_records_nojob(scheduler, source) -> None:
    found = _attribution_by_phase(scheduler, source)

    assert found["semantic"].job_id == "nojob:refresh_scheduler"


def test_submitted_worker_job_id_reaches_the_child_command(scheduler, tmp_path) -> None:
    """The real chain worker -> _execute_refresh -> _execute_refresh_impl ->
    _index_source; only git, alias I/O, the snapshot step and the subprocess
    runner are stood in."""
    source = Path(scheduler.golden_repos_dir) / "example-repo"
    (source / ".git").mkdir(parents=True)
    updater = MagicMock()
    updater.has_changes.return_value = True
    updater.get_source_path.return_value = str(source)
    commands: List[List[str]] = []

    def _fake_popen(*, command, phase_name, **_kw):
        commands.append(command)
        return 100

    scheduler._submit_refresh_job("example-repo-global", submitter_username="admin")
    worker = scheduler.background_job_manager.submit_job.call_args.kwargs["func"]
    assert "job_id" in inspect.signature(worker).parameters, (
        "BackgroundJobManager injects job_id only into a worker that declares it"
    )
    with (
        patch.object(scheduler.alias_manager, "read_alias", return_value=str(source)),
        patch.object(scheduler.alias_manager, "swap_alias"),
        patch.object(scheduler, "_create_snapshot", return_value=str(tmp_path / "s")),
        patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
        patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
            return_value=updater,
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_fake_popen,
        ),
    ):
        worker(job_id="job-9")

    stamped = [t for cmd in commands for t in cmd if t.startswith(_PREFIX)]
    assert stamped, f"no `cidx index` child was spawned: {commands}"
    assert {StatsAttribution.from_json(t[len(_PREFIX) :]).job_id for t in stamped} == {
        "job-9"
    }


def test_claimed_job_refresh_forwards_its_job_id(scheduler) -> None:
    with (
        patch.object(
            scheduler, "_resolve_global_alias", return_value="example-repo-global"
        ),
        patch.object(
            scheduler, "_execute_refresh", return_value={"success": True}
        ) as execute,
    ):
        scheduler.execute_refresh_for_claimed_job("example-repo", job_id="job-c1")

    assert execute.call_args.kwargs["job_id"] == "job-c1"
