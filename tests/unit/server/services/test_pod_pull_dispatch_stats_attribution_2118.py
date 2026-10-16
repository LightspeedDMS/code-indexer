"""Story #2118 slice 1a: a cluster pod-pull job keeps its real job id.

IndexJobClaimLoop claims a PENDING row and runs it through the pod-pull
dispatch table (``build_index_dispatch``, the table lifespan installs). The
claimed row's job id must reach the child's ``--stats-attribution``, never
``nojob:*``.

Real: the claim loop, the dispatch table, ``_provider_index_job``,
``GoldenRepoManager.execute_add_golden_repo_work`` and the command builders.
Stand-ins: the claimer (a recording double of DistributedJobClaimer), the
subprocess runner (captures the command), and cloning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services.embedding_stats_attribution import (
    StatsAttribution,
)
from code_indexer.server.services.index_job_claim_loop import IndexJobClaimLoop
from code_indexer.server.services.pod_pull_dispatch import build_index_dispatch

_PREFIX = "--stats-attribution="
_CLAIMED = "claimed-job-7"


class _Claimer:
    def __init__(self, job: Dict[str, Any]) -> None:
        self._job: Optional[Dict[str, Any]] = job
        self.completed: List[str] = []
        self.failed: List[str] = []

    def claim_next_job(self, **_kwargs: Any) -> Optional[Dict[str, Any]]:
        job, self._job = self._job, None
        return job

    def complete_job(self, job_id: str, result: Any = None) -> bool:
        self.completed.append(job_id)
        return True

    def fail_job(self, job_id: str, error: str) -> bool:
        self.failed.append(job_id)
        return True

    def update_progress(self, *args: Any, **kwargs: Any) -> bool:
        return True


def _run_claimed(op: str, metadata: Dict[str, Any], manager) -> List[List[str]]:
    commands: List[List[str]] = []

    def _fake_popen(*, command, **_kw):
        commands.append(command)
        return 100

    loop = IndexJobClaimLoop(
        claimer=_Claimer(
            {"job_id": _CLAIMED, "operation_type": op, "metadata": metadata}
        ),
        dispatch=build_index_dispatch(manager),
        node_id="node-a",
    )
    with (
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=_fake_popen,
        ),
        patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ),
    ):
        loop._process_one_job()
    return commands


def _stamped_job_ids(commands: List[List[str]]) -> set:
    return {
        StatsAttribution.from_json(t[len(_PREFIX) :]).job_id
        for c in commands
        for t in c
        if t.startswith(_PREFIX)
    }


def test_claimed_provider_index_job_records_its_job_id(tmp_path: Path) -> None:
    base_clone = tmp_path / "golden-repos" / "example-repo"
    (base_clone / ".code-indexer").mkdir(parents=True)
    manager = GoldenRepoManager(data_dir=str(tmp_path / "data"))

    commands = _run_claimed(
        "provider_index_add",
        {"repo_path": str(base_clone), "provider_name": "voyage-ai", "clear": False},
        manager,
    )

    assert commands, "the claimed provider-index job spawned no child"
    assert _stamped_job_ids(commands) == {_CLAIMED}


def test_claimed_add_golden_repo_job_records_its_job_id(tmp_path: Path) -> None:
    clone = tmp_path / "golden-repos" / "example-repo"
    clone.mkdir(parents=True)
    manager = GoldenRepoManager(data_dir=str(tmp_path / "data"))

    with (
        patch.object(manager, "_remove_orphan_clone_for_retry"),
        patch.object(manager, "_clone_repository", return_value=str(clone)),
    ):
        commands = _run_claimed(
            "add_golden_repo",
            {
                "repo_url": "local://example-repo",
                "alias": "example-repo",
                "default_branch": "main",
                "submitter_username": "admin",
            },
            manager,
        )

    assert commands, "the claimed add_golden_repo job spawned no child"
    assert _stamped_job_ids(commands) == {_CLAIMED}
