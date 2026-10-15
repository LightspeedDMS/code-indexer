"""Every reader/writer of an activated copy's index under a delegated
IndexStore goes through ``serve_index_here`` / ``publish_index``; the local
store keeps today's calls exactly.

* ``trigger_reindex``: delegated presence is the registry's answer for THIS
  user (the pod path does not exist); the job carries the owner, serves the
  recorded version first and publishes after a fully successful run; a fetch
  or publish failure fails the job with its cause. Local: today's disk check
  and job arguments.
* ``get_index_status``: delegated serves first; an unregistered copy is
  "not found", a store failure is an error.
* HNSW orphan sweep: a delegated activated candidate is served, repaired and
  the repair published; nothing is published for a clean one; local and
  golden candidates are repaired in place as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
)
from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.server.services.hnsw_orphan_sweep import scheduler as sweep
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
    SweepOutcome,
)
from code_indexer.server.startup import index_store_wiring
from code_indexer.utils.index_store import DelegatedIndexStore, LocalIndexStore
from tests.unit.server.index_store_fakes import FakeArtifacts

USER, ALIAS = "alice", "repo"


@pytest.fixture
def delegated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = DelegatedIndexStore(FakeArtifacts(tmp_path / "store"))
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        index_store_wiring, "server_index_store", lambda: LocalIndexStore()
    )


def _index_manager(tmp_path: Path) -> Any:
    manager = ActivatedRepoIndexManager.__new__(ActivatedRepoIndexManager)
    manager.data_dir = str(tmp_path / "data")
    manager.logger = MagicMock()
    manager.background_job_manager = MagicMock()
    manager.background_job_manager.list_jobs.return_value = {"jobs": []}
    manager.background_job_manager.submit_job.return_value = "job-1"
    arm = MagicMock()
    arm.get_activated_repo_path.return_value = str(
        tmp_path / "data" / "activated-repos" / USER / ALIAS
    )
    manager.activated_repo_manager = arm
    return manager


# ── trigger_reindex ─────────────────────────────────────────────────────────


def test_delegated_reindex_asks_the_registry_not_the_disk(
    tmp_path: Path, delegated: None
) -> None:
    manager = _index_manager(tmp_path)
    manager.activated_repo_manager.user_has_activated_repo.return_value = True
    assert manager.trigger_reindex(ALIAS, ["semantic"], False, USER) == "job-1"
    args = manager.background_job_manager.submit_job.call_args.args
    assert args[2:] == (
        ALIAS,
        manager.activated_repo_manager.get_activated_repo_path.return_value,
        ["semantic"],
        False,
        USER,
    )
    manager.activated_repo_manager.user_has_activated_repo.assert_called_once_with(
        USER, ALIAS
    )


def test_delegated_reindex_of_an_unregistered_copy_is_not_found(
    tmp_path: Path, delegated: None
) -> None:
    manager = _index_manager(tmp_path)
    manager.activated_repo_manager.user_has_activated_repo.return_value = False
    with pytest.raises(FileNotFoundError, match="not found for user"):
        manager.trigger_reindex(ALIAS, ["semantic"], False, USER)
    manager.background_job_manager.submit_job.assert_not_called()


def test_local_reindex_keeps_todays_disk_check_and_job_args(
    tmp_path: Path, local: None
) -> None:
    manager = _index_manager(tmp_path)
    with pytest.raises(FileNotFoundError, match="directory not found"):
        manager.trigger_reindex(ALIAS, ["semantic"], False, USER)
    Path(manager.activated_repo_manager.get_activated_repo_path.return_value).mkdir(
        parents=True
    )
    manager.trigger_reindex(ALIAS, ["semantic"], False, USER)
    assert len(manager.background_job_manager.submit_job.call_args.args) == 6
    manager.activated_repo_manager.user_has_activated_repo.assert_not_called()


def _job(manager: Any, owner: Any, results: Dict[str, Any]) -> Dict[str, Any]:
    manager._execute_all_index_types = lambda *a, **k: results
    return manager._execute_indexing_job(
        ALIAS, "/nonexistent/pod/path", ["semantic"], False, owner
    )


@pytest.fixture
def no_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_indexer.services import progress_subprocess_runner

    monkeypatch.setattr(
        progress_subprocess_runner, "gather_repo_metrics", lambda p: (1, 1)
    )


def test_the_job_serves_indexes_then_publishes(
    tmp_path: Path, delegated: None, no_metrics: None
) -> None:
    manager = _index_manager(tmp_path)
    arm = manager.activated_repo_manager
    result = _job(manager, USER, {"semantic": {"success": True}})
    assert result["success"] is True
    assert [
        c[0] for c in arm.method_calls if c[0] in ("serve_index_here", "publish_index")
    ] == [
        "serve_index_here",
        "publish_index",
    ]
    arm.serve_index_here.assert_called_once_with(USER, ALIAS)
    arm.publish_index.assert_called_once_with(USER, ALIAS)


def test_a_failed_index_type_publishes_nothing(
    tmp_path: Path, delegated: None, no_metrics: None
) -> None:
    manager = _index_manager(tmp_path)
    result = _job(manager, USER, {"semantic": {"success": False, "error": "boom"}})
    assert result["success"] is False
    manager.activated_repo_manager.publish_index.assert_not_called()


@pytest.mark.parametrize("step", ["serve_index_here", "publish_index"])
def test_a_store_failure_fails_the_job_with_its_cause(
    tmp_path: Path, delegated: None, no_metrics: None, step: str
) -> None:
    manager = _index_manager(tmp_path)
    getattr(manager.activated_repo_manager, step).side_effect = RuntimeError("EDIGEST")
    result = _job(manager, USER, {"semantic": {"success": True}})
    assert result["success"] is False
    assert "EDIGEST" in result["error"] and "IndexStore" in result["error"]


def test_a_job_without_an_owner_touches_no_store(
    tmp_path: Path, local: None, no_metrics: None
) -> None:
    manager = _index_manager(tmp_path)
    manager._execute_all_index_types = lambda *a, **k: {"semantic": {"success": True}}
    result = manager._execute_indexing_job(ALIAS, "/x", ["semantic"], False)
    assert result["success"] is True
    manager.activated_repo_manager.serve_index_here.assert_not_called()
    manager.activated_repo_manager.publish_index.assert_not_called()


# ── get_index_status ────────────────────────────────────────────────────────


def test_delegated_status_serves_first(tmp_path: Path, delegated: None) -> None:
    manager = _index_manager(tmp_path)
    served = tmp_path / "served"
    (served / ".code-indexer").mkdir(parents=True)
    manager.activated_repo_manager.serve_index_here.return_value = str(served)
    seen: List[Path] = []
    manager.get_index_status_for_path = lambda p, **k: seen.append(p) or {}
    manager.get_index_status(ALIAS, USER)
    manager.activated_repo_manager.serve_index_here.assert_called_once_with(USER, ALIAS)
    assert seen == [served]


def test_delegated_status_of_an_unregistered_copy_is_not_found(
    tmp_path: Path, delegated: None
) -> None:
    manager = _index_manager(tmp_path)
    manager.activated_repo_manager.serve_index_here.side_effect = ActivatedRepoError(
        "Repository 'repo' not found for user 'alice'"
    )
    with pytest.raises(FileNotFoundError):
        manager.get_index_status(ALIAS, USER)


def test_delegated_status_store_failure_is_an_error(
    tmp_path: Path, delegated: None
) -> None:
    manager = _index_manager(tmp_path)
    manager.activated_repo_manager.serve_index_here.side_effect = RuntimeError("down")
    with pytest.raises(RuntimeError, match="down"):
        manager.get_index_status(ALIAS, USER)


def test_local_status_does_not_serve(tmp_path: Path, local: None) -> None:
    manager = _index_manager(tmp_path)
    manager.get_index_status_for_path = lambda p, **k: {}
    manager.get_index_status(ALIAS, USER)
    manager.activated_repo_manager.serve_index_here.assert_not_called()


# ── HNSW orphan sweep ───────────────────────────────────────────────────────


class _Candidate:
    def __init__(self, kind: str, alias: str) -> None:
        self.kind = kind
        self.alias = alias
        self.sort_key = f"{kind}:{alias}"


def _scheduler(outcome: SweepOutcome) -> Any:
    instance = sweep.HNSWOrphanRepairSweepScheduler.__new__(
        sweep.HNSWOrphanRepairSweepScheduler
    )
    instance._activated_repo_manager = MagicMock()
    order: List[str] = []
    instance._activated_repo_manager.serve_index_here.side_effect = lambda *a: (
        order.append("serve")
    )
    instance._activated_repo_manager.publish_index.side_effect = lambda *a: (
        order.append("publish")
    )

    def process(candidate: Any) -> SweepOutcome:
        order.append("repair")
        return outcome

    instance._process_fn = process
    instance.order = order
    return instance


def test_a_delegated_activated_repair_is_published(delegated: None) -> None:
    scheduler = _scheduler(SweepOutcome.REPAIRED)
    assert (
        scheduler._process_one(_Candidate("activated", f"{USER}/{ALIAS}"))
        == SweepOutcome.REPAIRED
    )
    assert scheduler.order == ["serve", "repair", "publish"]
    scheduler._activated_repo_manager.publish_index.assert_called_once_with(USER, ALIAS)


def test_a_clean_delegated_activated_index_publishes_nothing(delegated: None) -> None:
    scheduler = _scheduler(SweepOutcome.CLEAN)
    scheduler._process_one(_Candidate("activated", f"{USER}/{ALIAS}"))
    assert scheduler.order == ["serve", "repair"]


@pytest.mark.parametrize("step", ["serve_index_here", "publish_index"])
def test_a_sweep_store_failure_is_an_error(delegated: None, step: str) -> None:
    scheduler = _scheduler(SweepOutcome.REPAIRED)
    getattr(scheduler._activated_repo_manager, step).side_effect = RuntimeError("x")
    assert (
        scheduler._process_one(_Candidate("activated", f"{USER}/{ALIAS}"))
        == SweepOutcome.ERROR
    )
    if step == "serve_index_here":
        assert "repair" not in scheduler.order


@pytest.mark.parametrize(
    "candidate",
    [_Candidate("golden", "repo-global"), _Candidate("activated", "no-slash")],
)
def test_other_delegated_candidates_are_repaired_in_place(
    delegated: None, candidate: Any
) -> None:
    scheduler = _scheduler(SweepOutcome.REPAIRED)
    scheduler._process_one(candidate)
    assert scheduler.order == ["repair"]


def test_local_activated_candidates_are_repaired_in_place(local: None) -> None:
    scheduler = _scheduler(SweepOutcome.REPAIRED)
    scheduler._process_one(_Candidate("activated", f"{USER}/{ALIAS}"))
    assert scheduler.order == ["repair"]


# ── REST readers: health resolution, FTS availability, semantic search ──────


def _health_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: Any) -> Any:
    from code_indexer.server.routers import repository_health as health

    golden = MagicMock()
    golden.get_golden_repo.return_value = None
    monkeypatch.setattr(health, "_get_golden_repo_manager", lambda: golden)
    monkeypatch.setattr(health, "_get_activated_repo_manager", lambda: arm)
    user = MagicMock()
    user.username = USER
    return health._resolve_repository_path(ALIAS, user)


def _arm_serving_into(path: Path) -> Any:
    arm = MagicMock()
    arm.get_activated_repo_path.return_value = str(path)
    arm.serve_index_here.side_effect = lambda u, a: (
        (path / ".code-indexer").mkdir(parents=True) or str(path)
    )
    return arm


def test_delegated_health_serves_the_copy_first(
    tmp_path: Path, delegated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "pod" / USER / ALIAS
    arm = _arm_serving_into(copy)
    assert _health_resolve(tmp_path, monkeypatch, arm) == (ALIAS, copy)
    arm.serve_index_here.assert_called_once_with(USER, ALIAS)


def test_delegated_health_of_an_unregistered_copy_is_404(
    tmp_path: Path, delegated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    arm = MagicMock()
    arm.get_activated_repo_path.return_value = str(tmp_path / "nope")
    arm.serve_index_here.side_effect = ActivatedRepoError("not found")
    with pytest.raises(HTTPException) as caught:
        _health_resolve(tmp_path, monkeypatch, arm)
    assert caught.value.status_code == 404


def test_delegated_health_store_failure_is_an_error(
    tmp_path: Path, delegated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    arm = MagicMock()
    arm.get_activated_repo_path.return_value = str(tmp_path / "nope")
    arm.serve_index_here.side_effect = RuntimeError("EDIGEST")
    with pytest.raises(RuntimeError, match="EDIGEST"):
        _health_resolve(tmp_path, monkeypatch, arm)


def test_local_health_does_not_serve(
    tmp_path: Path, local: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "local" / USER / ALIAS
    copy.mkdir(parents=True)
    arm = _arm_serving_into(copy)
    assert _health_resolve(tmp_path, monkeypatch, arm) == (ALIAS, copy)
    arm.serve_index_here.assert_not_called()


def test_delegated_fts_availability_is_judged_after_serving(
    tmp_path: Path, delegated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth import dependencies as auth_deps
    from code_indexer.server.routers.inline_query import register_query_routes

    copy = tmp_path / "pod" / USER / ALIAS
    arm = MagicMock()
    arm.list_activated_repositories.return_value = [
        {"user_alias": ALIAS, "username": USER, "is_global": False}
    ]
    arm.get_activated_repo_path.return_value = str(copy)
    arm.serve_index_here.side_effect = lambda u, a: (
        (copy / ".code-indexer" / "tantivy_index").mkdir(parents=True) or str(copy)
    )
    app = FastAPI()
    app.state.payload_cache = None
    app.state.access_filtering_service = None
    app.state.search_event_log_writer = None
    register_query_routes(
        app, semantic_query_manager=MagicMock(), activated_repo_manager=arm
    )
    user = MagicMock()
    user.username = USER
    app.dependency_overrides[auth_deps.get_current_user] = lambda: user
    cfg = MagicMock()
    cfg.get_config.return_value.node_id = "n"
    monkeypatch.setattr(
        "code_indexer.server.routers.inline_query.get_config_service",
        lambda: cfg,
        raising=False,
    )

    class _Tantivy:
        def __init__(self, index_dir: Any) -> None:
            pass

        def open_for_search(self) -> None:
            pass

        def search(self, **kwargs: Any) -> list:
            return []

    monkeypatch.setattr(
        "code_indexer.services.tantivy_index_manager.TantivyIndexManager", _Tantivy
    )
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/query",
        json={"query_text": "q", "repository_alias": ALIAS, "search_mode": "fts"},
    )
    assert response.status_code == 200, response.text
    arm.serve_index_here.assert_called_with(USER, ALIAS)


def test_a_delegated_activated_search_serves_the_copy_first(
    tmp_path: Path, delegated: None
) -> None:
    from unittest.mock import patch

    from code_indexer.server.query.semantic_query_manager import (
        SemanticQueryManager,
    )

    (tmp_path / "data" / "activated-repos").mkdir(parents=True)
    arm = MagicMock()
    arm.activated_repos_dir = str(tmp_path / "data" / "activated-repos")
    arm.get_activated_repo_path.return_value = "/nonexistent/pod/copy"
    arm.get_activation_id.return_value = "act-1"
    order: List[str] = []
    arm.serve_index_here.side_effect = lambda u, a: order.append("serve") or ""
    manager = SemanticQueryManager(
        data_dir=str(tmp_path / "data"),
        activated_repo_manager=arm,
        background_job_manager=MagicMock(),
    )
    with patch.object(
        manager,
        "_search_single_repository",
        side_effect=lambda *a, **k: order.append("search") or [],
    ):
        manager._perform_search(
            username=USER,
            user_repos=[{"user_alias": ALIAS}],
            query_text="q",
            limit=5,
            min_score=None,
            file_extensions=None,
        )
    assert order == ["serve", "search"]
    arm.serve_index_here.assert_called_once_with(USER, ALIAS)


def _recorded(order: List[str], step: str, result: Any) -> Any:
    """A side effect that records ``step`` and returns ``result``."""
    order.append(step)
    return result


    def serve(username: str, user_alias: str) -> str:
        (path / ".code-indexer").mkdir(parents=True)
        return str(path)


    def status_for(path: Path, **kwargs: Any) -> Dict[str, Any]:
        seen.append(path)
        return {}
