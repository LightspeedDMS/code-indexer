"""Bug #2039: the unified ci_* MCP handlers must not block the event loop.

Each of the six async ``ci_*`` handlers resolves the repository alias, the
caller's project access (a registry listing plus a grant lookup) and a
token before its first forge HTTP call. Those are synchronous storage reads;
the MCP dispatcher awaits async handlers on the loop itself, so running them
inline stalls every other request served by that worker.

The stand-ins below sit at the storage boundary only (registry rows, grant
service, token stores) plus the external forge HTTP client, and record the
thread that called them. The handler, the alias/forge resolution and the
access/token decision logic all run for real.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.server.mcp.handlers import (
    handle_ci_cancel_run,
    handle_ci_get_job_logs,
    handle_ci_get_run,
    handle_ci_list_runs,
    handle_ci_retry_run,
    handle_ci_search_logs,
    handle_gh_actions_cancel_run,
    handle_gh_actions_get_job_logs,
    handle_gh_actions_get_run,
    handle_gh_actions_list_runs,
    handle_gh_actions_retry_run,
    handle_gh_actions_search_logs,
    handle_gitlab_ci_cancel_pipeline,
    handle_gitlab_ci_get_job_logs,
    handle_gitlab_ci_get_pipeline,
    handle_gitlab_ci_list_pipelines,
    handle_gitlab_ci_retry_pipeline,
    handle_gitlab_ci_search_logs,
)

_ALIAS = "example-repo-global"
_REPO = {
    "alias_name": _ALIAS,
    "repo_url": "https://github.com/example-org/example-repo.git",
}
_GITLAB_REPO = {
    "alias_name": "example-project-global",
    "repo_url": "https://gitlab.example.com/example-group/example-project.git",
}
_REGISTRY = {r["alias_name"]: r for r in (_REPO, _GITLAB_REPO)}
_GH_ID = {"repository": "example-org/example-repo"}
_GL_ID = {"project_id": "example-group/example-project"}

# Every forge-client coroutine the handlers await (external HTTP boundary).
_CLIENT_METHODS = (
    "list_runs",
    "get_run",
    "get_job_logs",
    "search_logs",
    "cancel_run",
    "retry_run",
    "list_pipelines",
    "get_pipeline",
    "cancel_pipeline",
    "retry_pipeline",
)

# Legacy REST-route handlers: (handler, args, is_write).
_LEGACY_HANDLERS = [
    (handle_gh_actions_list_runs, dict(_GH_ID), False),
    (handle_gh_actions_get_run, {**_GH_ID, "run_id": 7}, False),
    (handle_gh_actions_get_job_logs, {**_GH_ID, "job_id": 9}, False),
    (handle_gh_actions_search_logs, {**_GH_ID, "run_id": 7, "pattern": "x"}, False),
    (handle_gh_actions_retry_run, {**_GH_ID, "run_id": 7}, True),
    (handle_gh_actions_cancel_run, {**_GH_ID, "run_id": 7}, True),
    (handle_gitlab_ci_list_pipelines, dict(_GL_ID), False),
    (handle_gitlab_ci_get_pipeline, {**_GL_ID, "pipeline_id": 7}, False),
    (handle_gitlab_ci_get_job_logs, {**_GL_ID, "job_id": 9}, False),
    (
        handle_gitlab_ci_search_logs,
        {**_GL_ID, "pipeline_id": 7, "pattern": "x"},
        False,
    ),
    (handle_gitlab_ci_retry_pipeline, {**_GL_ID, "pipeline_id": 7}, True),
    (handle_gitlab_ci_cancel_pipeline, {**_GL_ID, "pipeline_id": 7}, True),
]

# Responsiveness test timing. ci_list_runs performs at least 5 slow reads
# (get_repo, list_repos, access service, grants, token), so the handler
# spends >= 5 * _SLOW_STORE_DELAY_S = 250 ms in the store. Off the loop the
# ticker fires every _TICK_INTERVAL_S during that window (~25 ticks); on the
# loop it is frozen (0-1 ticks). The threshold sits far from both.
_SLOW_STORE_DELAY_S = 0.05
_TICK_INTERVAL_S = 0.01
_MIN_TICKS_WHILE_HANDLER_RUNS = 5

_HANDLERS = [
    (handle_ci_list_runs, {}, "list_runs", False),
    (handle_ci_get_run, {"run_id": "7"}, "get_run", False),
    (handle_ci_get_job_logs, {"job_id": "9"}, "get_job_logs", False),
    (
        handle_ci_search_logs,
        {"run_id": "7", "pattern": "error"},
        "search_logs",
        False,
    ),
    (handle_ci_cancel_run, {"run_id": "7"}, "cancel_run", True),
    (handle_ci_retry_run, {"run_id": "7"}, "retry_run", True),
]


class _ThreadRecorder:
    """Records (call_name, thread ident) for every storage read."""

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.calls: List[Tuple[str, int]] = []
        self._delay = delay_seconds

    def record(self, name: str) -> None:
        self.calls.append((name, threading.get_ident()))
        if self._delay:
            time.sleep(self._delay)  # a slow store, for the responsiveness test

    def threads(self) -> Set[int]:
        return {ident for _, ident in self.calls}

    def names(self) -> Set[str]:
        return {name for name, _ in self.calls}


def _user() -> MagicMock:
    user = MagicMock()
    user.username = "example-user"
    return user


def _store_patches(
    rec: _ThreadRecorder,
    registry: Optional[Dict[str, Dict[str, str]]] = None,
    granted: Optional[Set[str]] = None,
):
    rows = _REGISTRY if registry is None else registry
    grants = {"example-repo", "example-project"} if granted is None else granted

    def get_repo(alias: str) -> Any:
        rec.record("get_repo")
        return rows.get(alias)

    def list_repos() -> list:
        rec.record("list_repos")
        return list(rows.values())  # insertion order = registry scan order

    access_svc = MagicMock()
    access_svc.is_admin_user.return_value = False

    def accessible(username: str) -> Set[str]:
        rec.record("get_accessible_repos")
        return grants

    access_svc.get_accessible_repos.side_effect = accessible

    def access_service() -> Any:
        rec.record("access_service")
        return access_svc

    def resolve_token(platform: str) -> str:
        rec.record("global_token")
        return "read-token"

    def personal_credential(username: str, forge_host: str) -> Dict[str, str]:
        rec.record("personal_credential")
        return {"token": "write-token"}

    return (
        patch("code_indexer.server.mcp.handlers._get_global_repo", get_repo),
        patch("code_indexer.server.mcp.handlers._list_global_repos", list_repos),
        patch(
            "code_indexer.server.mcp.handlers._get_access_filtering_service",
            access_service,
        ),
        patch(
            "code_indexer.server.services.git_state_manager.TokenAuthenticator.resolve_token",
            staticmethod(resolve_token),
        ),
        patch(
            "code_indexer.server.mcp.handlers._get_personal_credential_for_host",
            personal_credential,
        ),
    )


def _forge_client() -> MagicMock:
    client = MagicMock()
    for name in _CLIENT_METHODS:
        setattr(client, name, AsyncMock(return_value=[]))
    client.last_rate_limit = None
    return client


async def _call_handler(
    handler,
    args,
    rec,
    registry: Optional[Dict[str, Dict[str, str]]] = None,
    granted: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    p1, p2, p3, p4, p5 = _store_patches(rec, registry, granted)
    with (
        p1,
        p2,
        p3,
        p4,
        p5,
        # External forge HTTP clients: the only network boundary.
        patch(
            "code_indexer.server.clients.github_actions_client.GitHubActionsClient",
            return_value=_forge_client(),
        ),
        patch(
            "code_indexer.server.clients.gitlab_ci_client.GitLabCIClient",
            return_value=_forge_client(),
        ),
    ):
        response = await handler(args, _user())
    return json.loads(response["content"][0]["text"])  # type: ignore[no-any-return]


def _run_on_loop(handler, args, rec) -> Tuple[Dict[str, Any], int]:
    """Run *handler* in a fresh event loop; return (payload, loop thread)."""
    loop_thread: Dict[str, int] = {}

    async def main() -> Dict[str, Any]:
        loop_thread["ident"] = threading.get_ident()
        return await _call_handler(handler, args, rec)

    data = asyncio.run(main())
    return data, loop_thread["ident"]


def _assert_one_off_loop_hop(rec: _ThreadRecorder, loop_ident: int) -> None:
    assert loop_ident not in rec.threads(), (
        f"storage reads ran on the event-loop thread: {rec.calls}"
    )
    # One off-loop hop per call, not one per read.
    assert len(rec.threads()) == 1, rec.calls


@pytest.mark.parametrize("handler,extra_args,method,is_write", _HANDLERS)
def test_store_reads_run_off_the_event_loop_thread(
    handler, extra_args, method, is_write
):
    rec = _ThreadRecorder()
    data, loop_ident = _run_on_loop(
        handler, {"repository_alias": _ALIAS, **extra_args}, rec
    )

    assert data["success"] is True, data
    expected = {
        "get_repo",
        "access_service",
        "get_accessible_repos",
        "personal_credential" if is_write else "global_token",
    }
    assert expected <= rec.names(), rec.calls
    _assert_one_off_loop_hop(rec, loop_ident)


# Two aliases sharing ONE clone URL; the ungranted one comes first in the
# registry scan. The pre-#2039 access decision is the scan's first URL match.
_SHARED_URL = "https://github.com/example-org/shared-repo.git"
_DUP_REGISTRY = {
    "shared-b-global": {"alias_name": "shared-b-global", "repo_url": _SHARED_URL},
    "shared-a-global": {"alias_name": "shared-a-global", "repo_url": _SHARED_URL},
}
# get_accessible_repos returns BASE names; the access check strips "-global"
# before comparing. This grants alias A (shared-a-global) only, not B.
_DUP_GRANTED = {"shared-a"}


@pytest.mark.parametrize(
    "handler,args",
    [
        (handle_ci_list_runs, {"repository_alias": "shared-a-global"}),
        (handle_ci_cancel_run, {"repository_alias": "shared-a-global", "run_id": 7}),
        (handle_gh_actions_list_runs, {"repository": "example-org/shared-repo"}),
        (
            handle_gh_actions_cancel_run,
            {"repository": "example-org/shared-repo", "run_id": 7},
        ),
    ],
)
def test_duplicate_clone_url_keeps_the_registry_scan_access_decision(handler, args):
    """Moving the reads off the loop must not change any access decision:
    the caller is refused exactly as the registry scan refused before."""
    rec = _ThreadRecorder()

    data = asyncio.run(
        _call_handler(handler, args, rec, registry=_DUP_REGISTRY, granted=_DUP_GRANTED)
    )

    assert data["success"] is False, data
    assert data["error"] == "Project 'example-org/shared-repo' not found.", data
    assert not {"global_token", "personal_credential"} & rec.names(), rec.calls


@pytest.mark.parametrize("handler,args,is_write", _LEGACY_HANDLERS)
def test_legacy_handler_store_reads_run_off_the_event_loop_thread(
    handler, args, is_write
):
    rec = _ThreadRecorder()
    data, loop_ident = _run_on_loop(handler, args, rec)

    assert data["success"] is True, data
    expected = {
        "list_repos",
        "access_service",
        "get_accessible_repos",
        "personal_credential" if is_write else "global_token",
    }
    assert expected <= rec.names(), rec.calls
    _assert_one_off_loop_hop(rec, loop_ident)


def test_event_loop_stays_responsive_during_slow_registry_read():
    rec = _ThreadRecorder(delay_seconds=_SLOW_STORE_DELAY_S)
    ticks: List[float] = []

    async def ticker(stop: asyncio.Event) -> None:
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(_TICK_INTERVAL_S)

    async def main() -> int:
        stop = asyncio.Event()
        tick_task = asyncio.create_task(ticker(stop))
        await asyncio.sleep(0)  # let the ticker start
        started = len(ticks)
        data = await _call_handler(
            handle_ci_list_runs, {"repository_alias": _ALIAS}, rec
        )
        progressed = len(ticks) - started
        stop.set()
        await tick_task
        assert data["success"] is True, data
        return progressed

    progressed = asyncio.run(main())

    assert progressed >= _MIN_TICKS_WHILE_HANDLER_RUNS, progressed
