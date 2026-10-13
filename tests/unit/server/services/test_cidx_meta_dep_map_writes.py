"""Dependency-map writes and coordination on a mount-less pod.

* Analysis, delta, refinement and repair -- every dependency-map WRITE, all
  driven by a Claude agent that reads the repositories from this pod's disk --
  are refused under a delegated content backend, before any job, sentinel,
  lock or file is touched (routes and the MCP trigger refuse before claiming).
* The coordination files that stay meaningful (the analysis sentinel, the
  dashboard cache's job slot) live in cidx-meta's shared content and keep
  their cluster-wide atomicity through the content writer's compare-and-swap.
* Under the local backend none of this applies.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.dep_map_delegated import (
    ANALYSIS_UNSUPPORTED,
    DependencyMapUnsupportedError,
    analysis_refusal,
)
from code_indexer.server.services.shared_job_sentinel import SharedJobSentinel
from tests.unit.server.cidx_meta_seam_fakes import MetaWorld


@pytest.fixture
def meta_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetaWorld:
    return MetaWorld(tmp_path, monkeypatch)


# ── analysis refused ─────────────────────────────────────────────────────


def _service(golden: Path) -> Any:
    from code_indexer.server.services.dependency_map_service import (
        DependencyMapService,
    )

    service = object.__new__(DependencyMapService)
    service._golden_repos_manager = SimpleNamespace(golden_repos_dir=str(golden))
    service._job_tracker = MagicMock(side_effect=AssertionError("job touched"))
    service._job_tracker.register_job_if_no_conflict.side_effect = AssertionError(
        "job registered"
    )
    service._refresh_scheduler = MagicMock()
    service._refresh_scheduler.acquire_write_lock.side_effect = AssertionError(
        "write lock taken"
    )
    return service


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.run_full_analysis(),
        lambda s: s.run_full_analysis(job_id="j", pre_claimed=True),
        lambda s: s.run_delta_analysis(),
        lambda s: s.run_tracked_refinement(),
        lambda s: s.run_refinement_cycle(),
        lambda s: s.run_graph_repair_dry_run(),
    ],
)
def test_every_analysis_entry_point_is_refused(meta_world: MetaWorld, call) -> None:
    meta_world.delegated()
    service = _service(meta_world.pod_golden)
    with meta_world.forbid_local_io():
        with pytest.raises(DependencyMapUnsupportedError) as exc:
            call(service)
    assert ANALYSIS_UNSUPPORTED in str(exc.value)
    service._refresh_scheduler.acquire_write_lock.assert_not_called()


def test_local_backend_is_not_refused(meta_world: MetaWorld) -> None:
    assert analysis_refusal() is None
    meta_world.delegated()
    assert analysis_refusal() == ANALYSIS_UNSUPPORTED


def test_scheduler_is_not_started(
    meta_world: MetaWorld, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    service = _service(meta_world.pod_golden)
    service._daemon_thread = None
    meta_world.delegated()
    with caplog.at_level(logging.ERROR):
        service.start_scheduler()
    assert service._daemon_thread is None
    assert any("scheduler not started" in r.message for r in caplog.records)


def _routes_service() -> Any:
    service = MagicMock()
    service.is_available.side_effect = AssertionError("sentinel consulted")
    return service


@pytest.mark.parametrize("route", ["trigger", "refinement", "repair"])
def test_routes_refuse_before_any_sentinel(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    from code_indexer.server.web import dependency_map_routes as routes

    service = _routes_service()
    monkeypatch.setattr(routes, "_require_admin_session", lambda request: object())
    monkeypatch.setattr(routes, "_get_dep_map_service_from_state", lambda: service)
    started = MagicMock(side_effect=AssertionError("thread started"))
    monkeypatch.setattr(routes.threading, "Thread", started)
    meta_world.delegated()
    request = SimpleNamespace()
    if route == "trigger":
        response = routes.trigger_dependency_map(request, mode="full")
    elif route == "refinement":
        response = routes.trigger_refinement(request)
    else:
        response = routes.trigger_dependency_map_repair(request)
    assert response.status_code == 501
    assert json.loads(response.body)["error"] == ANALYSIS_UNSUPPORTED
    service.is_available.assert_not_called()


def test_routes_unchanged_under_local(
    monkeypatch: pytest.MonkeyPatch, meta_world
) -> None:
    from code_indexer.server.web import dependency_map_routes as routes

    service = MagicMock()
    service.is_available.return_value = False
    monkeypatch.setattr(routes, "_require_admin_session", lambda request: object())
    monkeypatch.setattr(routes, "_get_dep_map_service_from_state", lambda: service)
    response = routes.trigger_refinement(SimpleNamespace())
    assert response.status_code == 409  # today's "already in progress"


def test_mcp_trigger_refuses_before_any_sentinel(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.mcp.handlers import _utils
    from code_indexer.server.mcp.handlers import admin

    service = _routes_service()
    monkeypatch.setattr(
        _utils,
        "app_module",
        SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(dependency_map_service=service))
        ),
    )
    config = SimpleNamespace(
        claude_integration_config=SimpleNamespace(dependency_map_enabled=True)
    )
    monkeypatch.setattr(
        "code_indexer.server.services.config_service.get_config_service",
        lambda: SimpleNamespace(get_config=lambda: config),
    )
    meta_world.delegated()
    response = admin.handle_trigger_dependency_analysis(
        {"mode": "full"},
        SimpleNamespace(username="admin", has_permission=lambda permission: True),
    )
    payload = json.loads(response["content"][0]["text"])
    assert payload["success"] is False and payload["error"] == ANALYSIS_UNSUPPORTED
    service.is_available.assert_not_called()


def test_lifespan_skips_dep_map_dir_work_only_when_delegated(
    meta_world: MetaWorld,
) -> None:
    from code_indexer.server.startup.lifespan import _dep_map_dir_is_on_this_disk

    assert _dep_map_dir_is_on_this_disk() is True
    meta_world.delegated()
    assert _dep_map_dir_is_on_this_disk() is False


# ── the analysis sentinel on shared content ──────────────────────────────


def _sentinel(world: MetaWorld) -> SharedJobSentinel:
    world.delegated()
    return SharedJobSentinel(world.pod_meta / "dependency-map", 60)


def test_shared_sentinel_protocol(meta_world: MetaWorld) -> None:
    sentinel = _sentinel(meta_world)
    with meta_world.forbid_local_io():
        first = sentinel.try_claim("analysis", "job-1", "node-a")
        second = sentinel.try_claim("analysis", "job-2", "node-b")
        active = sentinel.read_active("analysis")
        sentinel.release("analysis", expected_job_id="job-2")  # not the owner
        still = sentinel.read_active("analysis")
        sentinel.release("analysis", expected_job_id="job-1")
        gone = sentinel.read_active("analysis")
        third = sentinel.try_claim("analysis", "job-3", "node-b")
    assert first.success and not first.replaced_stale
    assert not second.success and second.active.job_id == "job-1"
    assert active.job_id == "job-1" and still.job_id == "job-1"
    assert gone is None and third.success
    lock = meta_world.remote_meta / "dependency-map" / "_active_analysis.lock"
    assert json.loads(lock.read_text())["job_id"] == "job-3"


def test_shared_sentinel_replaces_a_stale_holder(meta_world: MetaWorld) -> None:
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    meta_world.seed(
        "dependency-map/_active_analysis.lock",
        json.dumps(
            {"op_type": "analysis", "job_id": "dead", "node_id": "x", "started_at": old}
        ),
    )
    sentinel = _sentinel(meta_world)
    with meta_world.forbid_local_io():
        claim = sentinel.try_claim("analysis", "job-1", "node-a")
    assert claim.success and claim.replaced_stale


def test_shared_sentinel_stale_replace_is_compare_and_swap(
    meta_world: MetaWorld,
) -> None:
    """Two pods both see the stale holder; only the first replacement wins."""
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    meta_world.seed(
        "dependency-map/_active_analysis.lock",
        json.dumps(
            {"op_type": "analysis", "job_id": "dead", "node_id": "x", "started_at": old}
        ),
    )
    sentinel = _sentinel(meta_world)
    shared = sentinel._shared
    real = shared.read_with_sha

    def racing_read(path):
        data, sha = real(path)
        lock = meta_world.remote_meta / "dependency-map" / "_active_analysis.lock"
        lock.write_text(
            json.dumps(
                {
                    "op_type": "analysis",
                    "job_id": "other-pod",
                    "node_id": "y",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        return data, sha

    object.__setattr__(shared, "read_with_sha", racing_read)
    claim = sentinel.try_claim("analysis", "job-1", "node-a")
    assert not claim.success and claim.active.job_id == "other-pod"


def test_shared_sentinel_backend_down_raises(meta_world: MetaWorld) -> None:
    from code_indexer.server.services.cidx_meta_content import (
        CidxMetaUnavailableError,
    )

    sentinel = _sentinel(meta_world)
    meta_world.writer.fail = ConnectionError("down")
    with pytest.raises(CidxMetaUnavailableError):
        sentinel.try_claim("analysis", "job-1", "node-a")


def test_local_sentinel_unchanged(meta_world: MetaWorld) -> None:
    sentinel = SharedJobSentinel(meta_world.local_meta / "dependency-map", 60)
    assert sentinel.try_claim("analysis", "j", "n").success
    assert (meta_world.local_meta / "dependency-map" / "_active_analysis.lock").exists()


# ── the dashboard cache on shared content ────────────────────────────────


def _cache(world: MetaWorld) -> Any:
    from code_indexer.server.storage.filesystem_backends import (
        SharedContentDashboardCacheBackend,
        dashboard_cache_backend_for,
    )

    world.delegated()
    cache = dashboard_cache_backend_for(world.pod_meta / "dependency-map")
    assert isinstance(cache, SharedContentDashboardCacheBackend)
    return cache


def test_shared_dashboard_cache_round_trip(meta_world: MetaWorld) -> None:
    cache = _cache(meta_world)
    with meta_world.forbid_local_io():
        assert cache.get_cached() is None
        assert cache.claim_job_slot("job-1") is None
        assert cache.claim_job_slot("job-2") == "job-1"
        assert cache.set_job_slot("real-1", expected_current="job-1") is True
        assert cache.set_job_slot("x", expected_current="job-1") is False
        cache.set_cached('{"ok": true}')
        assert cache.is_fresh(60)
        cache.mark_job_failed("boom")
        cached = cache.get_cached()
    assert cached["result_json"] == '{"ok": true}'
    assert cached["last_failure_message"] == "boom"
    remote = meta_world.remote_meta / "dependency-map" / "_dashboard_cache.json"
    assert json.loads(remote.read_text())["job_id"] is None


def test_shared_dashboard_claim_is_compare_and_swap(meta_world: MetaWorld) -> None:
    """Another pod claims the empty slot between our read and our write."""
    cache = _cache(meta_world)
    remote = meta_world.remote_meta / "dependency-map" / "_dashboard_cache.json"
    real = cache._shared.read_with_sha

    def racing_read(path):
        result = real(path) if remote.exists() else None
        remote.parent.mkdir(parents=True, exist_ok=True)
        remote.write_text(json.dumps({"job_id": "other-pod"}))
        if result is None:
            raise FileNotFoundError(path)
        return result

    object.__setattr__(cache._shared, "read_with_sha", racing_read)
    assert cache.claim_job_slot("job-1") == "other-pod"
    assert json.loads(remote.read_text())["job_id"] == "other-pod"


def test_local_dashboard_cache_unchanged(meta_world: MetaWorld) -> None:
    from code_indexer.server.storage.filesystem_backends import (
        FilesystemDashboardCacheBackend,
        dashboard_cache_backend_for,
    )

    cache = dashboard_cache_backend_for(meta_world.local_meta / "dependency-map")
    assert type(cache) is FilesystemDashboardCacheBackend
    assert cache.claim_job_slot("j") is None


# ── X-Ray patterns ───────────────────────────────────────────────────────

_PATTERN = """name: {name}
description: d
language: python
evaluator_code: |
  fn evaluate_node(node) {{ false }}
"""


def _xray(meta: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from code_indexer.server.services import xray_pattern_service as xps

    git_calls: List[Any] = []
    monkeypatch.setattr(xps, "run_git_command", lambda *a, **k: git_calls.append(a))
    monkeypatch.setattr(
        xps,
        "get_config_service",
        lambda: SimpleNamespace(
            get_config=lambda: SimpleNamespace(cidx_meta_backup_config=None)
        ),
    )
    service = xps.XrayPatternService(meta)
    service.git_calls = git_calls  # type: ignore[attr-defined]
    return service


def _both(world: MetaWorld, run) -> tuple:
    world.local_mode()
    local = run(world.local_meta)
    pod_before = world.pod_tree()
    world.delegated()
    with world.forbid_local_io():
        delegated = run(world.pod_meta)
    world.local_mode()
    assert world.remote_tree() == world.local_tree()
    assert world.pod_tree() == pod_before
    return local, delegated


def test_xray_pattern_lifecycle_parity(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(meta: Path):
        service = _xray(meta, monkeypatch)
        service.ensure_seed_patterns()
        results = [
            service.store_xray_pattern("repo-a", _PATTERN.format(name="p1")),
            service.store_xray_pattern("repo-a", _PATTERN.format(name="p1")),
            service.store_xray_pattern(
                "repo-a", _PATTERN.format(name="p1"), overwrite=True
            ),
            service.store_xray_pattern("__any__", _PATTERN.format(name="p2")),
            service.delete_pattern("__any__", "p2"),
            service.delete_pattern("__any__", "p2"),
            service._load_pattern("repo-a", "p1") is not None,
            service._load_pattern("repo-b", "catch-rethrow") is not None,
            service._load_pattern("repo-b", "absent"),
        ]
        return results, len(service.git_calls) > 0

    (local, local_git), (delegated, delegated_git) = _both(meta_world, run)
    assert local == delegated
    assert local[1]["error"] == "pattern_already_exists"
    assert local[5]["error"] == "pattern_not_found"
    assert local_git is True  # the local path still commits
    assert delegated_git is False  # no git of shared content


def test_xray_store_race_is_already_exists(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta_world.delegated()
    service = _xray(meta_world.pod_meta, monkeypatch)
    target = meta_world.remote_meta / "xray-patterns" / "repo-a" / "p1.yaml"
    from code_indexer.server.services import cidx_meta_content as cmc

    original = cmc.SharedCidxMeta.exists

    def racing_exists(self, path):
        answer = original(self, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("name: theirs\n")
        return answer

    monkeypatch.setattr(cmc.SharedCidxMeta, "exists", racing_exists)
    result = service.store_xray_pattern("repo-a", _PATTERN.format(name="p1"))
    assert result["error"] == "pattern_already_exists"
    assert target.read_text() == "name: theirs\n"


def test_xray_load_fails_loud_when_backend_down(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.services.cidx_meta_content import (
        CidxMetaUnavailableError,
    )

    meta_world.delegated()
    service = _xray(meta_world.pod_meta, monkeypatch)
    meta_world.backend.fail = ConnectionError("down")
    with pytest.raises(CidxMetaUnavailableError):
        service._load_pattern("repo-a", "p1")


def test_shared_sentinel_release_rechecks_the_bytes(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another pod replaced the (stale) sentinel after our ownership check:
    the release re-reads and keeps the new holder."""
    from code_indexer.server.services.shared_job_sentinel import SentinelInfo

    sentinel = _sentinel(meta_world)
    assert sentinel.try_claim("analysis", "job-1", "node-a").success
    lock = meta_world.remote_meta / "dependency-map" / "_active_analysis.lock"
    lock.write_text(
        json.dumps(
            {
                "op_type": "analysis",
                "job_id": "other-pod",
                "node_id": "y",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    )
    stale_view = SentinelInfo("analysis", "job-1", "node-a", datetime.now(timezone.utc))
    monkeypatch.setattr(sentinel, "read_active", lambda op_type: stale_view)
    sentinel.release("analysis", expected_job_id="job-1")
    assert json.loads(lock.read_text())["job_id"] == "other-pod"


def test_xray_git_commit_is_a_no_op_on_shared_content(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta_world.delegated()
    service = _xray(meta_world.pod_meta, monkeypatch)
    with meta_world.forbid_local_io():
        service._git_commit(files=[meta_world.pod_meta / "x.yaml"], message="m")
    assert service.git_calls == []
    meta_world.local_mode()
    local = _xray(meta_world.local_meta, monkeypatch)
    local._git_commit(files=[meta_world.local_meta / "x.yaml"], message="m")
    assert len(local.git_calls) == 2  # add + commit, as before
