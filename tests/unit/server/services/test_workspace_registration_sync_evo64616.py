"""issue-64616: cidx registers agent workspaces from workspace-mcp's listing and
deregisters them only on an authoritative answer -- register, deregister,
listing failure (nothing deregistered), dry run, idempotence."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.services import workspace_registration_sync as ws
from code_indexer.server.services.job_tracker import DuplicateJobError
from code_indexer.server.services.workspace_registration_sync import (
    Listing,
    ListingUnavailableError,
    Workspace,
    WorkspaceSyncDeps,
    parse_listing,
    plan_workspaces,
    run_workspace_sync,
)

LIVE = Workspace("ws-0000000a", "ready", "/workspaces/ws-0000000a", "main")
CLAIMED = Workspace("ws-0000000b", "claimed", "/workspaces/ws-0000000b")
PROVISIONING = Workspace("ws-0000000c", "provisioning", "/workspaces/ws-0000000c")
NO_MOUNT = Workspace("ws-0000000d", "ready", None)
DESTROYED = Workspace("ws-0000000e", "destroyed", None)


def listing(*workspaces: Workspace, authoritative: bool = True) -> Listing:
    return Listing(authoritative, "postgres", list(workspaces))


# -- the plan --------------------------------------------------------------------


def test_live_workspaces_with_a_mount_are_registered() -> None:
    plan = plan_workspaces(
        listing(LIVE, CLAIMED, PROVISIONING, NO_MOUNT), [], max_deregister=20
    )
    assert [w.id for w in plan.register] == [LIVE.id, CLAIMED.id]
    assert plan.unregistrable == {NO_MOUNT.id: "the listing gives no mount_path"}


def test_registration_is_idempotent() -> None:
    plan = plan_workspaces(listing(LIVE), [LIVE.id], max_deregister=20)
    assert plan.register == [] and plan.deregister == []


def test_destroyed_and_absent_workspaces_are_deregistered() -> None:
    plan = plan_workspaces(
        listing(LIVE, DESTROYED),
        [LIVE.id, DESTROYED.id, "ws-0000000f"],
        max_deregister=20,
    )
    assert plan.deregister == [DESTROYED.id, "ws-0000000f"]


def test_a_listing_that_is_not_authoritative_deregisters_nothing() -> None:
    plan = plan_workspaces(
        listing(LIVE, DESTROYED, authoritative=False),
        [DESTROYED.id, "ws-0000000f"],
        max_deregister=20,
    )
    assert plan.deregister == [] and "not authoritative" in (plan.refused or "")


def test_an_empty_listing_deregisters_nothing() -> None:
    plan = plan_workspaces(listing(), ["ws-0000000f"], max_deregister=20)
    assert plan.deregister == [] and "empty" in (plan.refused or "")


def test_deregistrations_are_capped_per_pass() -> None:
    gone = [f"ws-000000{i:02x}" for i in range(16, 21)]
    plan = plan_workspaces(listing(LIVE), gone, max_deregister=2)
    assert plan.deregister == gone[:2] and plan.deferred_deregister == gone[2:]


def test_the_listing_is_parsed_strictly() -> None:
    parsed = parse_listing(
        {
            "authoritative": True,
            "registry_backend": "postgres",
            "workspaces": [
                {
                    "id": "ws-0000000a",
                    "status": "READY",
                    "mount_path": "/m",
                    "branch": "b",
                }
            ],
        }
    )
    assert parsed.workspaces == [Workspace("ws-0000000a", "ready", "/m", "b")]
    assert parse_listing({"workspaces": []}).authoritative is False
    with pytest.raises(ValueError):
        parse_listing({"workspaces": [{"status": "ready"}]})
    with pytest.raises(ValueError):
        parse_listing([])


# -- the pass ---------------------------------------------------------------------


class Tracker:
    def __init__(self, busy: bool = False) -> None:
        self.busy = busy
        self.done: List[str] = []

    def register_job_if_no_conflict(self, **job: Any) -> None:
        if self.busy:
            raise DuplicateJobError("workspace_registration_sync", "x", "j0")

    def complete_job(self, job_id: str, result: Any = None) -> None:
        self.done.append("complete")

    def fail_job(self, job_id: str, error: str = "") -> None:
        self.done.append("fail")


class Calls:
    def __init__(self) -> None:
        self.registered: List[str] = []
        self.deregistered: List[str] = []

    def register(self, workspace: Workspace) -> str:
        self.registered.append(workspace.id)
        return "job-r"

    def deregister(self, alias: str) -> str:
        self.deregistered.append(alias)
        return "job-d"


def deps(
    calls: Calls,
    lister: Any,
    registered: Optional[List[str]] = None,
    busy: bool = False,
) -> WorkspaceSyncDeps:
    return WorkspaceSyncDeps(
        list_workspaces=lister,
        registered_aliases=lambda: list(registered or []),
        register=calls.register,
        deregister=calls.deregister,
        job_tracker=Tracker(busy),
    )


def test_apply_registers_and_deregisters() -> None:
    calls = Calls()
    result = run_workspace_sync(
        deps(calls, lambda since: listing(LIVE, DESTROYED), [DESTROYED.id]), "apply"
    )
    assert calls.registered == [LIVE.id] and calls.deregistered == [DESTROYED.id]
    assert result["applied"]["failed"] == {}


def test_a_dry_run_changes_nothing() -> None:
    calls = Calls()
    result = run_workspace_sync(
        deps(calls, lambda since: listing(LIVE), ["ws-0000000f"]), "dry-run"
    )
    assert calls.registered == [] and calls.deregistered == []
    assert result["plan"]["register"] == [LIVE.id]
    assert result["plan"]["deregister"] == ["ws-0000000f"]


def test_a_listing_error_deregisters_nothing() -> None:
    calls = Calls()

    def broken(since: Optional[str]) -> Listing:
        raise RuntimeError("workspace-mcp GET /workspaces failed: HTTP 503")

    result = run_workspace_sync(deps(calls, broken, ["ws-0000000f"]), "apply")
    assert calls.deregistered == [] and calls.registered == []
    assert "503" in result["error"]


def test_without_the_listing_endpoint_it_only_reports() -> None:
    calls = Calls()

    def missing(since: Optional[str]) -> Listing:
        raise ListingUnavailableError("workspace-mcp has no GET /workspaces")

    result = run_workspace_sync(deps(calls, missing, ["ws-0000000f"]), "apply")
    assert calls.deregistered == [] and calls.registered == []
    assert "no GET /workspaces" in result["plan"]["report_only"]


def test_a_pass_running_elsewhere_is_skipped() -> None:
    calls = Calls()
    result = run_workspace_sync(
        deps(calls, lambda since: listing(LIVE), busy=True), "apply"
    )
    assert result["single_flight_skipped"] and calls.registered == []


def test_the_listing_asks_for_recently_destroyed_workspaces() -> None:
    asked: List[Optional[str]] = []

    def lister(since: Optional[str]) -> Listing:
        asked.append(since)
        return listing(LIVE)

    run_workspace_sync(deps(Calls(), lister), "dry-run")
    assert asked[0] is not None and asked[0][:4].isdigit()


# -- the HTTP client ---------------------------------------------------------------------


class Response:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        return self._body


def test_the_client_sends_the_bearer_and_reads_the_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    seen: Dict[str, Any] = {}

    def get(url: str, params: Dict[str, str], headers: Dict[str, str], timeout: float):
        seen.update(url=url, params=params, headers=headers)
        return Response(200, {"authoritative": True, "workspaces": []})

    monkeypatch.setattr(requests, "get", get)
    result = ws.WorkspaceMcpLister("http://wsm:3000/", "key").list("2026-01-01")
    assert result.authoritative is True
    assert seen["url"] == "http://wsm:3000/workspaces"
    assert seen["params"] == {"include_destroyed_since": "2026-01-01"}
    assert seen["headers"] == {"Authorization": "Bearer key"}


@pytest.mark.parametrize(
    "status, error", [(404, ListingUnavailableError), (500, RuntimeError)]
)
def test_the_client_maps_failures(
    monkeypatch: pytest.MonkeyPatch, status: int, error: type
) -> None:
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(status))
    with pytest.raises(error):
        ws.WorkspaceMcpLister("http://wsm", "key").list(None)


def test_a_quarantined_workspace_is_live() -> None:
    quarantined = Workspace("ws-00000010", "quarantined", "/w")
    plan = plan_workspaces(
        listing(LIVE, quarantined), [quarantined.id], max_deregister=20
    )
    assert plan.deregister == [] and plan.register == [LIVE]


def test_the_token_comes_from_env_or_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv(ws.TOKEN_ENV, raising=False)
    monkeypatch.delenv(ws.TOKEN_FILE_ENV, raising=False)
    assert ws.workspaces_token() is None
    secret = tmp_path / "token"
    secret.write_text("from-file\n")
    monkeypatch.setenv(ws.TOKEN_FILE_ENV, str(secret))
    assert ws.workspaces_token() == "from-file"
    monkeypatch.setenv(ws.TOKEN_ENV, "from-env")
    assert ws.workspaces_token() == "from-env"


@pytest.mark.parametrize("status", [401, 503])
def test_a_refused_listing_is_an_error_not_report_only(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(status))
    calls = Calls()
    lister = ws.WorkspaceMcpLister("http://wsm", "key")
    result = run_workspace_sync(deps(calls, lister.list, ["ws-0000000f"]), "apply")
    assert str(status) in result["error"] and calls.deregistered == []


# -- which workspace-mcp, and whether cidx can read the workspace ------------------


class _Manager:
    """The slice of GoldenRepoManager ``deps_from_manager`` uses."""

    def __init__(self, golden_repos_dir: str) -> None:
        self.golden_repos_dir = golden_repos_dir
        self.job_tracker = None
        self.added: List[Dict[str, Any]] = []

    def list_golden_repos(self) -> List[Dict[str, Any]]:
        return []

    def add_golden_repo(self, **kwargs: Any) -> str:
        self.added.append(kwargs)
        return "job"

    def remove_golden_repo(self, alias: str, **kwargs: Any) -> str:
        return "job"


@pytest.fixture
def sync_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (ws.ENDPOINT_ENV, ws.TOKEN_ENV, ws.TOKEN_FILE_ENV):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _delegated(monkeypatch: pytest.MonkeyPatch, delegated: bool) -> None:
    from code_indexer.server.startup import content_backend_wiring as wiring

    monkeypatch.setattr(wiring, "is_content_seam_delegated", lambda: delegated)


def test_the_listing_uses_its_own_url_never_the_content_seam(
    sync_env: pytest.MonkeyPatch,
) -> None:
    """workspace_mcp.endpoint (the content seam) is never consulted: the
    listing goes to CIDX_WORKSPACE_SYNC_WORKSPACE_MCP_URL with its own token."""
    import requests

    from code_indexer.server.services import config_service

    def no_config() -> Any:
        raise AssertionError("the content seam's config was read")

    sync_env.setattr(config_service, "get_config_service", no_config)
    sync_env.setenv(ws.ENDPOINT_ENV, "http://provisioner.example:3000/")
    sync_env.setenv(ws.TOKEN_ENV, "listing-token")
    seen: Dict[str, Any] = {}

    def get(url: str, params: Any, headers: Dict[str, str], timeout: float) -> Any:
        seen.update(url=url, headers=headers)
        return Response(200, {"authoritative": True, "workspaces": []})

    sync_env.setattr(requests, "get", get)
    ws.deps_from_manager(_Manager("/golden")).list_workspaces(None)
    assert seen["url"] == "http://provisioner.example:3000/workspaces"
    assert seen["headers"] == {"Authorization": "Bearer listing-token"}


def test_without_its_url_the_sync_only_reports_and_says_why(
    sync_env: pytest.MonkeyPatch,
) -> None:
    sync_env.setenv(ws.TOKEN_ENV, "listing-token")
    calls = Calls()
    sync = ws.deps_from_manager(_Manager("/golden"))
    result = run_workspace_sync(
        deps(calls, sync.list_workspaces, ["ws-0000000f"]), "apply"
    )
    assert ws.ENDPOINT_ENV in result["plan"]["report_only"]
    assert "workspace_mcp.endpoint" in result["plan"]["report_only"]
    assert calls.registered == [] and calls.deregistered == []


def test_without_a_token_the_sync_only_reports(sync_env: pytest.MonkeyPatch) -> None:
    sync_env.setenv(ws.ENDPOINT_ENV, "http://provisioner.example:3000")
    with pytest.raises(ListingUnavailableError, match=ws.TOKEN_ENV):
        ws.deps_from_manager(_Manager("/golden")).list_workspaces(None)


@pytest.mark.parametrize(
    "mount, reachable",
    [
        ("/golden/ws-0000000a", True),  # provisioned in place
        ("/golden/ws-0000000a/", True),
        ("/workspaces/ws-0000000a", False),  # the provisioner's own volume
        ("/golden/ws-0000000b", False),  # in place, but another id's directory
    ],
)
def test_delegated_content_registers_only_in_place_workspaces(
    mount: str, reachable: bool
) -> None:
    reason = ws.unreachable_reason(
        Workspace("ws-0000000a", "ready", mount),
        golden_repos_dir="/golden",
        delegated=True,
    )
    assert (reason is None) is reachable
    if not reachable:
        assert "content_backend='delegated'" in str(reason)


def test_local_content_needs_the_path_on_this_pod(tmp_path: Any) -> None:
    here = tmp_path / "ws-0000000a"
    here.mkdir()
    assert (
        ws.unreachable_reason(
            Workspace("ws-0000000a", "ready", str(here)),
            golden_repos_dir="/golden",
            delegated=False,
        )
        is None
    )
    assert "not visible" in str(
        ws.unreachable_reason(
            Workspace("ws-0000000a", "ready", str(tmp_path / "gone")),
            golden_repos_dir="/golden",
            delegated=False,
        )
    )


def test_an_unreachable_workspace_is_reported_never_added(
    sync_env: pytest.MonkeyPatch,
) -> None:
    _delegated(sync_env, True)
    manager = _Manager("/golden")
    sync = ws.deps_from_manager(manager)
    in_place = Workspace("ws-0000000a", "ready", "/golden/ws-0000000a")
    elsewhere = Workspace("ws-0000000b", "ready", "/workspaces/ws-0000000b")
    sync.list_workspaces = lambda since: listing(in_place, elsewhere)
    sync.job_tracker = Tracker(False)

    result = run_workspace_sync(sync, "apply")

    assert [a["alias"] for a in manager.added] == ["ws-0000000a"]
    assert result["plan"]["register"] == ["ws-0000000a"]
    assert "not in place" in result["plan"]["unregistrable"]["ws-0000000b"]
