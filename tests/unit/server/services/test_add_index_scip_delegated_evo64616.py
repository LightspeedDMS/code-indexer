"""EVO-64616: add_golden_repo_index {index_type: scip} under all-delegated
backends (tools-test, cidx 0f9ca76).

* evolution / evolution-3: the job "completed" with no build behind it, and
  scip stayed not_indexed. An explicit request now always enqueues a build
  (clear=True); a build that cannot be enqueued fails the job; an existing
  configuration is "already initialised", never a "❌".
* evolution-4: the build was enqueued, then the job failed creating a local
  snapshot ("index directory not created"). A delegated SCIP build builds
  nothing on this pod, so there is nothing to snapshot or publish.
* The build's progress and outcome are tracked (job tracker, shared across
  pods): get_index_status shows BUILDING / FAILED (with why), and an empty
  SCIP query says why instead of 0 results.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple, cast
from unittest.mock import MagicMock, patch

import pytest

from cidx_scip.server import runtime
from tests.unit.server._scip_feature import scip_pipeline_installed
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoError,
    GoldenRepoManager,
)
from cidx_scip.server import build_status as status_module
from code_indexer.server.services.job_tracker import DuplicateJobError
from cidx_scip.server import lifecycle as scip_lifecycle
from cidx_scip.server.build_status import (
    OPERATION,
    BuildTracker,
    explain_empty_results,
    latest_build,
    with_build_state,
)
from cidx_scip.server.lifecycle import (
    ScipDelegationOutcome,
    funnel_scip_generation,
)
from cidx_scip.server.resolution_queue import (
    DelegatedScipBuild,
    DelegatedScipBuildQueue,
)

pytestmark = pytest.mark.xfail(reason="v12.103.0 snapshot: upstream API changes", strict=False)

_FUNNEL = "cidx_scip.server.lifecycle.funnel_golden_scip"
DELEGATED = SimpleNamespace(scip_backend="delegated-plugin")
LOCAL = SimpleNamespace(scip_backend="local")


class _ScipSection:
    """The scip feature's ``config`` capability, reduced to its section."""

    def __init__(self, scip_backend: str) -> None:
        self._values = {"scip_backend": scip_backend}

    def section(self) -> Dict[str, object]:
        return dict(self._values)


class _ScipContext:
    """A started scip feature's context holding only ``config``."""

    def __init__(self, scip_backend: str) -> None:
        self._config = _ScipSection(scip_backend)

    def has_capability(self, name: str) -> bool:
        return name == "config"

    def capability(self, name: str) -> object:
        return self._config


@contextmanager
def _scip_backend(config: Any) -> Iterator[None]:
    """``scip_backend`` as the started scip feature reads it (its section's
    bootstrap key, modular phase 4d), with its index builder installed."""
    runtime.CONTEXT.install(cast(Any, _ScipContext(config.scip_backend)))
    try:
        with scip_pipeline_installed():
            yield
    finally:
        runtime.CONTEXT.uninstall()


# -- a job tracker ------------------------------------------------------------------


class FakeTracker:
    def __init__(self) -> None:
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.order: List[str] = []

    def register_job(
        self, job_id: str, operation_type: str, username: str, **kwargs: Any
    ) -> None:
        self.jobs[job_id] = {
            "job_id": job_id,
            "operation_type": operation_type,
            "status": "pending",
            "repo_alias": kwargs.get("repo_alias"),
            "created_at": f"t{len(self.order)}",
        }
        self.order.append(job_id)

    def register_job_if_no_conflict(
        self,
        job_id: str,
        operation_type: str,
        username: str,
        repo_alias: str,
        **kwargs: Any,
    ) -> None:
        # Like the shared table: one active row per (operation, alias).
        for job in self.jobs.values():
            if (
                job["operation_type"] == operation_type
                and job["repo_alias"] == repo_alias
                and job["status"] in ("pending", "running")
            ):
                raise DuplicateJobError(operation_type, repo_alias, job["job_id"])
        self.register_job(
            job_id, operation_type, username, repo_alias=repo_alias, **kwargs
        )

    def cancel_job(self, job_id: str) -> None:
        self.jobs[job_id]["status"] = "cancelled"

    def get_job(self, job_id: str) -> Any:
        return None

    def update_status(
        self, job_id: str, status: Optional[str] = None, **_: Any
    ) -> None:
        self.jobs[job_id]["status"] = status

    def complete_job(self, job_id: str, result: Any = None) -> None:
        self.jobs[job_id].update(status="completed", result=result)

    def fail_job(self, job_id: str, error: str = "") -> None:
        self.jobs[job_id].update(status="failed", error=error)

    def query_jobs(
        self, operation_type: str, repo_alias: str, limit: int
    ) -> List[Dict[str, Any]]:
        rows = [
            self.jobs[j]
            for j in reversed(self.order)
            if self.jobs[j]["operation_type"] == operation_type
            and self.jobs[j]["repo_alias"] == repo_alias
        ]
        return rows[:limit]


def _result(ok: bool, stderr: str = "") -> Any:
    from cidx_scip.discovery import DiscoveredProject
    from cidx_scip.generator import GenerationResult, ProjectGenerationResult
    from cidx_scip.indexers.base import IndexerResult, IndexerStatus

    project = ProjectGenerationResult(
        project=DiscoveredProject(
            relative_path=Path("svc"),
            language="java",
            build_system="gradle",
            build_file=Path("svc/build.gradle"),
        ),
        indexer_result=IndexerResult(
            status=IndexerStatus.SUCCESS if ok else IndexerStatus.FAILED,
            duration_seconds=1.0,
            output_file=None,
            stdout="",
            stderr=stderr,
            exit_code=0 if ok else 2,
        ),
    )
    return GenerationResult(
        total_projects=1,
        successful_projects=1 if ok else 0,
        failed_projects=0 if ok else 1,
        project_results=[project],
    )


def _build(
    tracker: FakeTracker, outcome: Any
) -> Tuple[DelegatedScipBuild, BuildTracker]:
    build_tracker = BuildTracker(tracker, "evolution", "evolution@abc")

    def run() -> Any:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    build = DelegatedScipBuild(
        alias="evolution",
        dedupe_key="evolution@abc",
        run_build=run,
        on_state=build_tracker.on_state,
    )
    return build, build_tracker


# -- the build's state, visible beyond this pod's log --------------------------------


def test_a_successful_build_is_completed_with_its_summary() -> None:
    tracker = FakeTracker()
    build, build_tracker = _build(tracker, _result(True))
    queue = DelegatedScipBuildQueue(sleep=lambda _s: None)
    assert queue.enqueue(build, start_worker=False)
    build_tracker.queued()
    assert tracker.jobs[build_tracker.job_id or ""]["status"] == "pending"
    queue.drain_pending()
    job = tracker.jobs[build_tracker.job_id or ""]
    assert job["status"] == "completed"
    assert job["result"]["successful_projects"] == 1
    assert job["operation_type"] == OPERATION and job["repo_alias"] == "evolution"


def test_a_build_in_progress_is_running() -> None:
    tracker = FakeTracker()
    seen: List[str] = []
    build_tracker = BuildTracker(tracker, "evolution", "evolution@abc")

    def run() -> Any:
        seen.append(tracker.jobs[build_tracker.job_id or ""]["status"])
        return _result(True)

    build = DelegatedScipBuild(
        alias="evolution",
        dedupe_key="evolution@abc",
        run_build=run,
        on_state=build_tracker.on_state,
    )
    queue = DelegatedScipBuildQueue(sleep=lambda _s: None)
    queue.enqueue(build, start_worker=False)
    build_tracker.queued()
    queue.drain_pending()
    assert seen == ["running"]


def test_a_failed_build_records_why() -> None:
    tracker = FakeTracker()
    build, build_tracker = _build(tracker, _result(False, "gradle: build failed\nexit"))
    queue = DelegatedScipBuildQueue(sleep=lambda _s: None)
    queue.enqueue(build, start_worker=False)
    queue.drain_pending()
    job = tracker.jobs[build_tracker.job_id or ""]
    assert job["status"] == "failed" and "svc: exit" in job["error"]


def test_a_build_that_raises_records_why() -> None:
    tracker = FakeTracker()
    build, build_tracker = _build(tracker, RuntimeError("sandbox unreachable"))
    queue = DelegatedScipBuildQueue(sleep=lambda _s: None)
    queue.enqueue(build, start_worker=False)
    queue.drain_pending()
    job = tracker.jobs[build_tracker.job_id or ""]
    assert job["status"] == "failed" and "sandbox unreachable" in job["error"]


def test_a_tracker_failure_never_touches_the_build() -> None:
    class Broken(FakeTracker):
        def register_job(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("db down")

    build, _ = _build(Broken(), _result(True))
    queue = DelegatedScipBuildQueue(sleep=lambda _s: None)
    succeeded: List[Any] = []
    build.on_success = succeeded.append
    queue.enqueue(build, start_worker=False)
    queue.drain_pending()
    assert len(succeeded) == 1


def test_the_funnel_tracks_an_enqueued_build_but_not_a_deduped_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
    tracker = FakeTracker()
    queue = MagicMock()
    queue.enqueue.side_effect = [True, False]
    manager = SimpleNamespace(job_tracker=tracker)
    generator = SimpleNamespace(generate=lambda: _result(True))
    for expected in (ScipDelegationOutcome.ENQUEUED, ScipDelegationOutcome.DEDUPED):
        out = funnel_scip_generation(
            alias="evolution",
            repo_root=tmp_path,
            config=DELEGATED,
            clear=True,
            golden_repo_manager=manager,
            build_queue=queue,
            generator_builder=lambda *a, **k: generator,
        )
        assert out is expected
    assert len(tracker.jobs) == 1, "a deduped build is the one already tracked"


# -- status and queries say what happened ----------------------------------------------


def _tracked(state: str, error: Optional[str] = None) -> FakeTracker:
    tracker = FakeTracker()
    tracker.register_job("j1", OPERATION, "system-scip", repo_alias="evolution")
    tracker.jobs["j1"]["status"] = state
    if error:
        tracker.jobs["j1"]["error"] = error
    return tracker


@pytest.mark.parametrize(
    "state, shown",
    [("pending", "BUILDING"), ("running", "BUILDING"), ("failed", "FAILED")],
)
def test_get_index_status_shows_the_build(
    monkeypatch: pytest.MonkeyPatch, state: str, shown: str
) -> None:
    monkeypatch.setattr(
        status_module, "server_job_tracker", lambda: _tracked(state, "gradle failed")
    )
    status = with_build_state(
        {"status": "not_indexed", "project_count": 0}, "evolution-global"
    )
    assert status["status"] == shown
    assert status["delegated_build"]["job_id"] == "j1"
    if shown == "FAILED":
        assert status["error"] == "gradle failed"


def test_a_readable_index_keeps_its_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        status_module, "server_job_tracker", lambda: _tracked("running")
    )
    status = with_build_state({"status": "SUCCESS", "project_count": 2}, "evolution")
    assert status["status"] == "SUCCESS" and "delegated_build" in status


def test_no_tracked_build_leaves_the_status_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(status_module, "server_job_tracker", lambda: FakeTracker())
    assert with_build_state({"status": "not_indexed"}, "x") == {"status": "not_indexed"}
    assert latest_build(None, "x") is None


@pytest.mark.parametrize(
    "state, text",
    [("running", "still being built"), ("failed", "failed (job j1): why")],
)
def test_an_empty_scip_query_says_why(
    monkeypatch: pytest.MonkeyPatch, state: str, text: str
) -> None:
    monkeypatch.setattr(
        status_module, "server_job_tracker", lambda: _tracked(state, "why")
    )
    assert text in (explain_empty_results("evolution-global") or "")

    from cidx_scip.server.mcp import scip as scip_handlers

    monkeypatch.setattr(scip_handlers, "_index_gap", lambda alias, username: None)
    payload: Dict[str, Any] = {"success": True, "results": []}
    scip_handlers._explain_no_results(payload, "evolution-global", "admin")
    assert text in payload["scip_index"]
    found: Dict[str, Any] = {"success": True, "results": [{"symbol": "x"}]}
    scip_handlers._explain_no_results(found, "evolution-global", "admin")
    assert "scip_index" not in found


def test_a_completed_build_explains_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        status_module, "server_job_tracker", lambda: _tracked("completed")
    )
    assert explain_empty_results("evolution") is None


# -- the add-index job ------------------------------------------------------------------


def _manager(tmp_path: Path) -> Tuple[GoldenRepoManager, str, MagicMock]:
    mgr = GoldenRepoManager(data_dir=str(tmp_path))
    repo_path = tmp_path / "golden-repos" / "evolution"
    repo_path.mkdir(parents=True)
    created = datetime.now(timezone.utc).isoformat()
    url = "https://example.com/g/evolution.git"
    mgr.golden_repos["evolution"] = GoldenRepo(
        alias="evolution",
        repo_url=url,
        default_branch="main",
        clone_path=str(repo_path),
        created_at=created,
    )
    mgr._sqlite_backend.add_repo(
        alias="evolution",
        repo_url=url,
        default_branch="main",
        clone_path=str(repo_path),
        created_at=created,
        enable_temporal=False,
        temporal_options=None,
    )
    mgr.background_job_manager = MagicMock()
    scheduler = MagicMock()
    scheduler.acquire_write_lock.return_value = True
    mgr._refresh_scheduler = scheduler
    return mgr, str(repo_path), scheduler


def _run_add_index(
    mgr: GoldenRepoManager, repo_path: str, index_types: List[str], init_rc: int
) -> Dict[str, Any]:
    outcome: Dict[str, Any] = {}

    def submit(operation_type: str, func: Any, **kwargs: Any) -> str:
        def run(cmd: List[str], **kw: Any) -> Any:
            if cmd[:2] == ["cidx", "init"]:
                text = (
                    "❌ Configuration already exists at .../config.json"
                    if init_rc
                    else "ok"
                )
                return SimpleNamespace(returncode=init_rc, stdout=text, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("subprocess.run", side_effect=run),
            patch.object(mgr, "get_actual_repo_path", return_value=repo_path),
            patch(
                "code_indexer.server.repositories.golden_repo_manager._gather_repo_metrics",
                return_value=(1, 1),
            ),
        ):
            outcome["result"] = func()
        return "job-1"

    with patch.object(mgr.background_job_manager, "submit_job", side_effect=submit):
        mgr.add_indexes_to_golden_repo(
            "evolution", index_types, submitter_username="admin"
        )
    return dict(outcome["result"])


def test_a_delegated_scip_add_enqueues_and_never_snapshots(tmp_path: Path) -> None:
    mgr, repo_path, scheduler = _manager(tmp_path)
    with (
        _scip_backend(DELEGATED),
        patch(_FUNNEL, return_value=ScipDelegationOutcome.ENQUEUED) as funnel,
    ):
        result = _run_add_index(mgr, repo_path, ["scip"], init_rc=1)
    assert funnel.call_args.kwargs["clear"] is True
    scheduler._create_snapshot.assert_not_called()
    scheduler.alias_manager.swap_alias.assert_not_called()
    assert result["success"] is True
    assert "enqueued" in result["message"] and "asynchronously" in result["message"]
    assert "[init] already initialised" in result["stdout"]
    assert "❌" not in result["stdout"]
    scheduler.release_write_lock.assert_called_once()


def test_a_delegated_scip_add_that_cannot_enqueue_fails_the_job(tmp_path: Path) -> None:
    mgr, repo_path, scheduler = _manager(tmp_path)
    with (
        _scip_backend(DELEGATED),
        patch(_FUNNEL, return_value=ScipDelegationOutcome.SKIPPED),
    ):
        with pytest.raises(GoldenRepoError, match="could not be enqueued"):
            _run_add_index(mgr, repo_path, ["scip"], init_rc=1)
    scheduler._create_snapshot.assert_not_called()


def test_a_local_index_type_still_snapshots(tmp_path: Path) -> None:
    mgr, repo_path, scheduler = _manager(tmp_path)
    scheduler._create_snapshot.return_value = str(tmp_path / ".versioned/evolution/v_1")
    scheduler.published_swap_extra.return_value = {}
    scheduler.alias_manager.read_alias.return_value = None
    with (
        _scip_backend(LOCAL),
        patch(_FUNNEL, return_value=ScipDelegationOutcome.ENQUEUED),
    ):
        _run_add_index(mgr, repo_path, ["fts", "scip"], init_rc=0)
    scheduler._create_snapshot.assert_called_once()


def test_the_local_scip_backend_still_builds_here() -> None:
    with _scip_backend(LOCAL):
        assert GoldenRepoManager._add_index_builds_here(["scip"]) is True
    with _scip_backend(DELEGATED):
        assert GoldenRepoManager._add_index_builds_here(["scip"]) is False
        assert GoldenRepoManager._add_index_builds_here(["semantic"]) is True
