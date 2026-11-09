"""issue-64616: cidx registers agent workspaces itself (the content service no longer
registers at provision or deregisters at destroy).

the content service registered each live agent workspace as a golden repository
named by its id (``ws-<8 hex>``), from the workspace's files
(``file://<mount_path>``, branch ``master``, description ``Workspace <id>``)
so agents can search their own workspace, and removed it when the workspace
was destroyed. This pass does the same from the content service's generic listing,
``GET {CIDX_WORKSPACE_SYNC_WORKSPACE_MCP_URL}/workspaces`` ->
``{authoritative, registry_backend, workspaces: [{id, status, repo_url,
branch, mount_path, created_at, updated_at, destroyed_at?}]}``:

* **register** a live workspace (ready / claimed / idle) that has a
  ``mount_path`` and no golden repository yet, through cidx's add path;
* **deregister** a ``ws-*`` golden repository ONLY when the listing is
  ``authoritative`` (a postgres registry, or a single replica) and the
  workspace is listed as destroyed, or is absent (cidx registered it, so it
  was seen before). Every other status -- ``quarantined`` included -- is
  live. A listing that is not
  authoritative, failed, or came back empty while workspaces are registered
  deregisters nothing; at most :data:`DEFAULT_MAX_DEREGISTER` per pass.
* a the content service without the endpoint (404), no listing URL configured
  (``CIDX_WORKSPACE_SYNC_WORKSPACE_MCP_URL``), or no token configured
  (``CIDX_WORKSPACE_MCP_WORKSPACES_TOKEN`` or ``..._TOKEN_FILE``): report
  only. A refusal (401, 503) or any other failure is an error: nothing is
  deregistered.

WHICH the content service. The agent workspaces live in the the content service that
provisions them, which is NOT necessarily the one cidx reads repository
content through. ``the content service.endpoint`` names the latter (the content
seam / clone backend), which holds no agent workspaces, so the listing has its
own URL and never falls back to it.

REACHABILITY. A workspace's ``mount_path`` is a path on the provisioning
service's volume. Registering it means cidx can read it:

* local content backend: the path must exist on this pod (the add copies it);
* delegated content backend: cidx reads only through its content seam and
  git seam, at the golden repository's own path, so the workspace must be
  provisioned IN PLACE at ``<golden_repos_dir>/<id>`` (the add refuses any
  other local source). A workspace elsewhere is reported ``unregistrable``
  with that reason instead of submitting an add job that cannot succeed.
  See docs/workspace-registration-sync.md for the design that would make
  out-of-place workspaces indexable.

Mode ``CIDX_WORKSPACE_SYNC_MODE`` = ``off`` (default) | ``dry-run`` |
``apply``; single-flight across the cluster like the catalog sync.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

OPERATION = "workspace_registration_sync"
SENTINEL_ALIAS = "__workspace_registration_sync__"
SUBMITTER = "system-workspace-sync"
MODE_ENV = "CIDX_WORKSPACE_SYNC_MODE"
#: the content service's WSM_WORKSPACES_SHARED_SECRET (its own secret, not the
#: clone-path one): the value, or a file holding it (a mounted secret).
TOKEN_ENV = "CIDX_WORKSPACE_MCP_WORKSPACES_TOKEN"
#: Base URL of the the content service that provisions the agent workspaces (serves
#: ``GET /workspaces``). Deliberately separate from ``the content service.endpoint``.
ENDPOINT_ENV = "CIDX_WORKSPACE_SYNC_WORKSPACE_MCP_URL"
TOKEN_FILE_ENV = "CIDX_WORKSPACE_MCP_WORKSPACES_TOKEN_FILE"
INTERVAL_ENV = "CIDX_WORKSPACE_SYNC_INTERVAL_SECONDS"
MAX_DEREGISTER_ENV = "CIDX_WORKSPACE_SYNC_MAX_DEREGISTER_PER_PASS"
LOOKBACK_ENV = "CIDX_WORKSPACE_SYNC_DESTROYED_LOOKBACK_DAYS"
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_MAX_DEREGISTER = 20
DEFAULT_LOOKBACK_DAYS = 30

WORKSPACE_ALIAS = re.compile(r"^ws-[0-9a-f]{8}$")
REGISTERABLE = ("ready", "claimed", "idle")
DESTROYED = "destroyed"
DEFAULT_BRANCH = "master"


class ListingUnavailableError(RuntimeError):
    """the content service has no ``GET /workspaces`` (an older version)."""


@dataclass(frozen=True)
class Workspace:
    id: str
    status: str
    mount_path: Optional[str] = None
    branch: Optional[str] = None


@dataclass(frozen=True)
class Listing:
    authoritative: bool
    registry_backend: str
    workspaces: List[Workspace]


def parse_listing(body: Any) -> Listing:
    """The listing, or ValueError for a body that is not one."""
    if not isinstance(body, dict) or not isinstance(body.get("workspaces"), list):
        raise ValueError("the workspace listing has no workspaces list")
    workspaces = []
    for item in body["workspaces"]:
        if not isinstance(item, dict) or not item.get("id") or not item.get("status"):
            raise ValueError(f"a listed workspace has no id/status: {item!r}")
        workspaces.append(
            Workspace(
                str(item["id"]),
                str(item["status"]).lower(),
                item.get("mount_path") or None,
                item.get("branch") or None,
            )
        )
    return Listing(
        body.get("authoritative") is True,
        str(body.get("registry_backend") or "unknown"),
        workspaces,
    )


class WorkspaceMcpLister:
    """``GET {endpoint}/workspaces`` with the content service's bearer."""

    def __init__(self, endpoint: str, api_key: str, timeout: float = 30.0) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def list(self, destroyed_since: Optional[str]) -> Listing:
        import requests

        params = {"include_destroyed_since": destroyed_since} if destroyed_since else {}
        response = requests.get(
            f"{self._endpoint}/workspaces",
            params=params,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self._timeout,
        )
        if response.status_code == 404:
            raise ListingUnavailableError(
                "the content service has no GET /workspaces; workspace sync reports only"
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"the content service GET /workspaces failed: HTTP {response.status_code}"
            )
        return parse_listing(response.json())


@dataclass
class WorkspacePlan:
    register: List[Workspace] = field(default_factory=list)
    deregister: List[str] = field(default_factory=list)
    deferred_deregister: List[str] = field(default_factory=list)
    unregistrable: Dict[str, str] = field(default_factory=dict)
    refused: Optional[str] = None
    report_only: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "register": [w.id for w in self.register],
            "deregister": list(self.deregister),
            "deferred_deregister": list(self.deferred_deregister),
            "unregistrable": dict(self.unregistrable),
            "deregister_refused": self.refused,
            "report_only": self.report_only,
        }


def plan_workspaces(
    listing: Listing,
    registered: List[str],
    *,
    max_deregister: int,
    unreachable: Optional[Callable[[Workspace], Optional[str]]] = None,
) -> WorkspacePlan:
    """What to register and deregister. Pure.

    *unreachable* says why cidx could not read a workspace it would register
    (None when it can); such a workspace is ``unregistrable``, never added.
    """
    plan = WorkspacePlan()
    known = set(registered)
    listed = {w.id: w for w in listing.workspaces}
    for workspace in sorted(listing.workspaces, key=lambda w: w.id):
        if workspace.id in known or workspace.status not in REGISTERABLE:
            continue
        if not WORKSPACE_ALIAS.match(workspace.id):
            plan.unregistrable[workspace.id] = "not a ws-<8 hex> id"
        elif not workspace.mount_path:
            plan.unregistrable[workspace.id] = "the listing gives no mount_path"
        elif unreachable is not None and (reason := unreachable(workspace)):
            plan.unregistrable[workspace.id] = reason
        else:
            plan.register.append(workspace)
    gone = sorted(
        alias
        for alias in known
        if alias not in listed or listed[alias].status == DESTROYED
    )
    if not gone:
        return plan
    if not listing.authoritative:
        plan.refused = (
            f"the listing is not authoritative (registry {listing.registry_backend}): "
            f"{len(gone)} workspace(s) not deregistered"
        )
    elif not listing.workspaces:
        plan.refused = (
            f"the listing is empty while {len(known)} workspace(s) are registered: "
            "nothing deregistered"
        )
    else:
        plan.deregister = gone[:max_deregister]
        plan.deferred_deregister = gone[max_deregister:]
    return plan


@dataclass
class WorkspaceSyncDeps:
    list_workspaces: Callable[[Optional[str]], Listing]
    registered_aliases: Callable[[], List[str]]
    register: Callable[[Workspace], Any]
    deregister: Callable[[str], Any]
    job_tracker: Any = None
    max_deregister: int = DEFAULT_MAX_DEREGISTER
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    #: Why cidx cannot read a workspace (None: it can). See module docstring.
    unreachable: Optional[Callable[[Workspace], Optional[str]]] = None


def run_workspace_sync(deps: WorkspaceSyncDeps, mode: str) -> Dict[str, Any]:
    """One pass in *mode* (``dry-run`` or ``apply``); single-flight."""
    from code_indexer.server.services.job_tracker import DuplicateJobError
    from code_indexer.server.services.single_flight import claim, release

    if mode not in ("dry-run", "apply"):
        return {"mode": mode, "error": f"mode {mode!r} runs nothing"}
    try:
        job_id = claim(deps.job_tracker, OPERATION, SENTINEL_ALIAS, SUBMITTER)
    except DuplicateJobError:
        return {"mode": mode, "single_flight_skipped": True}
    result: Dict[str, Any] = {"mode": mode}
    try:
        since = (
            datetime.now(timezone.utc) - timedelta(days=deps.lookback_days)
        ).isoformat()
        try:
            listing = deps.list_workspaces(since)
        except ListingUnavailableError as exc:
            result["plan"] = WorkspacePlan(report_only=str(exc)).as_dict()
            release(deps.job_tracker, job_id, result=result)
            logger.warning("Workspace sync: %s", exc)
            return result
        plan = plan_workspaces(
            listing,
            deps.registered_aliases(),
            max_deregister=deps.max_deregister,
            unreachable=deps.unreachable,
        )
        result["plan"] = plan.as_dict()
        result["authoritative"] = listing.authoritative
        if plan.refused:
            logger.error("Workspace sync: %s", plan.refused)
        if plan.unregistrable:
            logger.warning(
                "Workspace sync: %d workspace(s) not registrable: %s",
                len(plan.unregistrable),
                "; ".join(f"{k}: {v}" for k, v in sorted(plan.unregistrable.items())),
            )
        if mode == "apply":
            result["applied"] = _apply(deps, plan)
    except Exception as exc:
        # A listing error deregisters NOTHING: the plan is never applied.
        result["error"] = f"{type(exc).__name__}: {exc}"
        release(deps.job_tracker, job_id, error=result["error"])
        logger.error("Workspace sync failed; nothing deregistered: %s", result["error"])
        return result
    release(deps.job_tracker, job_id, result=result)
    return result


def _apply(deps: WorkspaceSyncDeps, plan: WorkspacePlan) -> Dict[str, Any]:
    applied: Dict[str, Any] = {"registered": {}, "deregistered": {}, "failed": {}}
    for workspace in plan.register:
        try:
            applied["registered"][workspace.id] = str(deps.register(workspace))
        except Exception as exc:
            applied["failed"][f"register:{workspace.id}"] = (
                f"{type(exc).__name__}: {exc}"
            )
    for alias in plan.deregister:
        try:
            applied["deregistered"][alias] = str(deps.deregister(alias))
        except Exception as exc:
            applied["failed"][f"deregister:{alias}"] = f"{type(exc).__name__}: {exc}"
    return applied


def workspaces_token() -> Optional[str]:
    """The listing's bearer: the env value, else the file it names. Never
    logged."""
    import os

    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    path = os.environ.get(TOKEN_FILE_ENV, "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError as exc:
        logger.error(
            "Cannot read %s (%s): %s", TOKEN_FILE_ENV, path, type(exc).__name__
        )
        return None


def listing_endpoint() -> Optional[str]:
    """The provisioning the content service's base URL, from
    ``CIDX_WORKSPACE_SYNC_WORKSPACE_MCP_URL``; None when unset. Never the
    content seam's ``the content service.endpoint``."""
    import os

    return os.environ.get(ENDPOINT_ENV, "").strip() or None


def unreachable_reason(
    workspace: Workspace, *, golden_repos_dir: str, delegated: bool
) -> Optional[str]:
    """Why cidx could not read *workspace*'s files, or None when it can.

    Delegated content: only a workspace provisioned in place at
    ``<golden_repos_dir>/<id>`` is reachable (the content and git seams read
    the golden repository's own path; the add refuses any other local
    source). Local content: the mount path must exist on this pod.
    """
    import os

    mount = os.path.normpath(str(workspace.mount_path))
    if delegated:
        in_place = os.path.normpath(os.path.join(golden_repos_dir, workspace.id))
        if mount != in_place:
            return (
                f"mount_path {mount} is not in place at {in_place}: with "
                "content_backend='delegated' cidx reads repositories only "
                "through its content and git seams at the golden repository's "
                "own path, which do not serve the provisioning service's "
                "workspace volume"
            )
        return None
    if not os.path.isdir(mount):
        return f"mount_path {mount} is not visible on this pod"
    return None


def deps_from_manager(manager: Any) -> WorkspaceSyncDeps:
    """A pass over the running server: the provisioning the content service from
    ``CIDX_WORKSPACE_SYNC_WORKSPACE_MCP_URL`` and its own bearer."""
    from code_indexer.server.services.golden_catalog_sync import positive_int_env
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated,
    )

    def listing(since: Optional[str]) -> Listing:
        endpoint = listing_endpoint()
        if not endpoint:
            raise ListingUnavailableError(
                f"{ENDPOINT_ENV} is not set (the the content service that provisions "
                "the agent workspaces; the content service.endpoint is the content "
                "seam's and is not used); workspace sync reports only"
            )
        token = workspaces_token()
        if not token:
            raise ListingUnavailableError(
                f"no workspace listing token ({TOKEN_ENV} / {TOKEN_FILE_ENV}); "
                "workspace sync reports only"
            )
        return WorkspaceMcpLister(endpoint, token).list(since)

    def unreachable(workspace: Workspace) -> Optional[str]:
        return unreachable_reason(
            workspace,
            golden_repos_dir=str(manager.golden_repos_dir),
            delegated=is_content_seam_delegated(),
        )

    def registered() -> List[str]:
        return [
            str(row["alias"])
            for row in manager.list_golden_repos()
            if WORKSPACE_ALIAS.match(str(row.get("alias") or ""))
        ]

    def register(workspace: Workspace) -> Any:
        return manager.add_golden_repo(
            repo_url=f"file://{workspace.mount_path}",
            alias=workspace.id,
            default_branch=workspace.branch or DEFAULT_BRANCH,
            description=f"Workspace {workspace.id}",
            submitter_username=SUBMITTER,
        )

    def deregister(alias: str) -> Any:
        return manager.remove_golden_repo(alias, submitter_username=SUBMITTER)

    return WorkspaceSyncDeps(
        list_workspaces=listing,
        registered_aliases=registered,
        register=register,
        deregister=deregister,
        job_tracker=getattr(manager, "job_tracker", None),
        max_deregister=positive_int_env(MAX_DEREGISTER_ENV, DEFAULT_MAX_DEREGISTER),
        lookback_days=positive_int_env(LOOKBACK_ENV, DEFAULT_LOOKBACK_DAYS),
        unreachable=unreachable,
    )
