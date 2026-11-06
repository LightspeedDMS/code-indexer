"""EVO-64616: cidx keeps its golden-repository catalog in sync with GitLab
itself (the workspace-mcp seeder is gone).

One pass:

1. **Catalog.** Every project of the configured GitLab groups (subgroups
   included; archived and deletion-scheduled projects left out) plus the
   explicitly listed repository URLs. A listing failure aborts the pass:
   names are computed from the whole catalog, never from part of it.
2. **Names.** Deterministic shortest-unique path aliases
   (:func:`catalog_aliases`): a unique leaf stays bare; a shared leaf takes
   the shortest namespace suffix that makes it unique, joined with ``-``
   (``conversion-evolution``, ``innovation-week-evolution``). No numeric
   suffix, and the result depends only on the set of projects.
3. **Identity.** A registered clone's project is decided by its content
   (:func:`content_identity`), by the refresh's own rule: its origin when
   HEAD is on origin's branch AFTER fetching it, otherwise the repository it
   was cloned from (and its origin is healed back to that). A clone is
   never re-pointed to a different project.
4. **Plan.** Heals, then renames of ``<leaf>-N`` and misassigned aliases
   (``services/golden_repo_rename.py``: no re-clone, no re-embed, the URL and
   branch corrected to the content), ordered so every target name is free
   when its rename runs, then adds of new projects through the normal add
   path (bounded per pass). An alias already right for its content keeps
   its name. Projects that vanished upstream are REPORTED, never removed:
   removal stays an explicit admin action. An EMPTY upstream project (no
   commit, so no branch to clone) is reported as a skipped add, and an add
   whose last job failed is not resubmitted until upstream changes or
   :data:`FAILED_ADD_RETRY_ENV` has passed (:func:`failed_add_holds`).

Scheduled as a cluster-wide single-flight job (:data:`OPERATION`); the
mode is ``CIDX_GOLDEN_CATALOG_SYNC_MODE`` = ``off`` (default) | ``dry-run`` |
``apply``. ``dry-run`` reports the plan and changes nothing.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from code_indexer.utils.git_remote_url import parse_git_remote_url

logger = logging.getLogger(__name__)

OPERATION = "golden_catalog_sync"
SENTINEL_ALIAS = "__golden_catalog_sync__"
SUBMITTER = "system-catalog-sync"

MODE_ENV = "CIDX_GOLDEN_CATALOG_SYNC_MODE"
INTERVAL_ENV = "CIDX_GOLDEN_CATALOG_SYNC_INTERVAL_SECONDS"
GROUPS_ENV = "CIDX_GOLDEN_CATALOG_GITLAB_GROUPS"
REPOS_ENV = "CIDX_GOLDEN_CATALOG_REPOS"
HOST_ENV = "CIDX_GOLDEN_CATALOG_GITLAB_HOST"
TOKEN_ENV = "CIDX_GOLDEN_CATALOG_GITLAB_TOKEN"
MAX_ADDS_ENV = "CIDX_GOLDEN_CATALOG_MAX_ADDS_PER_PASS"
FAILED_ADD_RETRY_ENV = "CIDX_GOLDEN_CATALOG_FAILED_ADD_RETRY_SECONDS"
DEFAULT_INTERVAL_SECONDS = 1800
DEFAULT_MAX_ADDS = 10
DEFAULT_FAILED_ADD_RETRY_SECONDS = 24 * 3600
#: The job a planned add becomes (``GoldenRepoManager.add_golden_repo``).
ADD_OPERATION = "add_golden_repo"
#: How many recent add jobs one pass reads to find each alias's last one.
_ADD_HISTORY_LIMIT = 500
EMPTY_PROJECT_REASON = (
    "empty upstream project (no commit, no default branch): nothing to clone "
    "until the first push"
)
MODES = ("off", "dry-run", "apply")

_WORKSPACE_ALIAS = re.compile(r"^ws-[0-9a-f]{8}$")
_DELETION_SCHEDULED = re.compile(r"-deletion_scheduled-\d+(\.git)?$")
#: Aliases the catalog never manages (cidx's own, and agent workspaces).
_UNMANAGED = ("cidx-meta",)


# -- configuration ------------------------------------------------------------


def _env_list(name: str) -> List[str]:
    return [
        item.strip() for item in os.environ.get(name, "").split(",") if item.strip()
    ]


def sync_mode(env: str = MODE_ENV) -> str:
    """The configured mode; ``off`` when unset or not one of :data:`MODES`."""
    mode = os.environ.get(env, "off").strip().lower()
    if mode not in MODES:
        logger.warning("%s=%r is not one of %s; treated as off", env, mode, MODES)
        return "off"
    return mode


def positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        return default
    return value if value > 0 else default


# -- naming -------------------------------------------------------------------


def _segment(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", text).strip("-_")
    return cleaned or "repo"


def catalog_aliases(paths: Iterable[str]) -> Dict[str, str]:
    """Each project path (``group/sub/leaf``) -> its alias: the shortest
    trailing run of path segments, joined with ``-``, that no other path in
    the set shares (case-insensitively). When names clash, the shorter one
    keeps it and the longer ones grow; equal ones all grow. Depends only on
    the SET of paths."""
    segments = {
        path: [_segment(part) for part in path.strip("/").split("/") if part]
        for path in sorted(set(paths))
    }
    depth = {path: 1 for path in segments}

    def name(path: str) -> str:
        alias = "-".join(segments[path][-depth[path] :])
        return f"{alias}-repo" if alias.endswith("-global") else alias

    while True:
        by_name: Dict[str, List[str]] = {}
        for path in segments:
            by_name.setdefault(name(path).casefold(), []).append(path)
        grew = False
        for clash in by_name.values():
            if len(clash) < 2:
                continue
            # A shorter name keeps priority: only the longer ones grow (a
            # bare unique leaf is never pushed out by a joined name).
            shortest = min(depth[path] for path in clash)
            longer = [path for path in clash if depth[path] > shortest]
            for path in longer or clash:
                if depth[path] < len(segments[path]):
                    depth[path] += 1
                    grew = True
        if not grew:
            return {path: name(path) for path in segments}


def is_legacy_alias(alias: str, leaf: str) -> bool:
    """The seeder's arrival-order name: ``<leaf>-<N>`` (N >= 2)."""
    match = re.fullmatch(re.escape(_segment(leaf)) + r"-(\d+)", alias)
    return match is not None and int(match[1]) >= 2


# -- the catalog ----------------------------------------------------------------


@dataclass(frozen=True)
class CatalogProject:
    """One upstream repository: its identity (``host/path``), credential-free
    clone URL, path and default branch (None: the remote's HEAD). ``empty``:
    GitLab says it holds no commit; ``last_activity_at``: GitLab's ISO time
    of its last change (None when unknown)."""

    identity: str
    url: str
    path: str
    default_branch: Optional[str] = None
    empty: bool = False
    last_activity_at: Optional[str] = None

    @property
    def leaf(self) -> str:
        return self.path.rstrip("/").rsplit("/", 1)[-1]


def project_for_url(
    url: str,
    branch: Optional[str] = None,
    *,
    empty: bool = False,
    last_activity_at: Optional[str] = None,
) -> Optional[CatalogProject]:
    parsed = parse_git_remote_url(url)
    if parsed is None:
        return None
    return CatalogProject(
        parsed.identity.casefold(),
        _clean(url),
        parsed.repo_path,
        branch,
        empty,
        last_activity_at,
    )


def _clean(url: str) -> str:
    from code_indexer.server.git.git_subprocess_env import (
        remote_url_without_credentials,
    )

    return remote_url_without_credentials(url)


class GitLabGroupLister:
    """``GET /api/v4/groups/<group>/projects`` with subgroups, every page,
    archived and deletion-scheduled projects left out (the seeder's
    filters). Any failure raises: a partial catalog is never used. Empty
    projects stay IN the catalog, marked ``empty``: they still take part in
    naming (names depend on the whole set), they are just never added."""

    def __init__(self, host: str, token: str, timeout: float = 30.0) -> None:
        self._base = f"https://{host}/api/v4"
        self._token = token
        self._timeout = timeout

    def projects(self, group: str) -> List[CatalogProject]:
        import urllib.parse

        import httpx

        found: List[CatalogProject] = []
        page = 1
        while True:
            response = httpx.get(
                f"{self._base}/groups/{urllib.parse.quote(group, safe='')}/projects",
                params={
                    "include_subgroups": "true",
                    "archived": "false",
                    "per_page": 100,
                    "page": page,
                    "order_by": "id",
                    "sort": "asc",
                },
                headers={"PRIVATE-TOKEN": self._token},
                timeout=self._timeout,
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"GitLab group {group!r} listing failed: HTTP "
                    f"{response.status_code}"
                )
            items = response.json()
            if not isinstance(items, list):
                raise RuntimeError(f"GitLab group {group!r} listing is not a list")
            for item in items:
                url = item.get("http_url_to_repo") or ""
                if (
                    not url
                    or item.get("archived")
                    or item.get("marked_for_deletion_on")
                    or _DELETION_SCHEDULED.search(url)
                ):
                    continue
                # An empty project has no commit and GitLab reports no
                # default_branch for it; cloning one fails ("Remote branch
                # master not found") on every pass (EVO-64616).
                project = project_for_url(
                    url,
                    item.get("default_branch") or None,
                    empty=bool(item.get("empty_repo")),
                    last_activity_at=item.get("last_activity_at") or None,
                )
                if project is not None:
                    found.append(project)
            total_pages = response.headers.get("x-total-pages")
            if not items or (total_pages and page >= int(total_pages)):
                return found
            page += 1


def gitlab_token() -> Optional[str]:
    """``CIDX_GOLDEN_CATALOG_GITLAB_TOKEN``, else the GitLab token in cidx's CI
    token store (the one repository discovery uses)."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    try:
        from code_indexer.server.web.routes import _get_token_manager

        data = _get_token_manager().get_token("gitlab")
    except Exception as exc:
        logger.warning("No GitLab token in the CI token store: %s", exc)
        return None
    return getattr(data, "token", None) if data else None


def load_catalog(
    groups: Sequence[str],
    repos: Sequence[str],
    lister: Optional[GitLabGroupLister],
) -> Dict[str, CatalogProject]:
    """identity -> project. Raises when a group cannot be listed."""
    catalog: Dict[str, CatalogProject] = {}
    if groups and lister is None:
        raise RuntimeError(
            f"{GROUPS_ENV} is set but no GitLab token is configured "
            f"({TOKEN_ENV} or the CI token store)"
        )
    if lister is not None:
        for group in groups:
            for listed in lister.projects(group):
                catalog.setdefault(listed.identity, listed)
    for url in repos:
        explicit = project_for_url(url)
        if explicit is None:
            raise RuntimeError(f"{REPOS_ENV} entry {url!r} is not a git remote URL")
        catalog.setdefault(explicit.identity, explicit)
    return catalog


# -- content identity -------------------------------------------------------------


@dataclass
class ContentIdentity:
    """What a clone holds. ``heal_to``: the origin to restore (the clone
    source) when origin was repointed and HEAD did not follow it."""

    identity: Optional[str]
    url: Optional[str] = None
    branch: Optional[str] = None
    heal_to: Optional[str] = None
    why_unknown: str = ""


def content_identity(
    git: Any, clone: str, credentials_url: Optional[str] = None
) -> ContentIdentity:
    """What *clone* holds, decided by the same rule the refresh heals by
    (``repointed_origin``):

    * origin == the ``clone: from`` source: never repointed -- origin;
    * otherwise HEAD's branch is fetched from origin
      (:func:`~code_indexer.global_repos.repointed_origin.content_followed_origin`,
      so a stale ``refs/remotes/origin/<b>`` never decides): content on
      origin's history is origin's; content that is not is the clone
      source's, and origin is healed back to it;
    * an empty clone (unborn HEAD) has nothing to have followed: it is its
      clone source's (healed back to it if origin was repointed), else its
      origin's;
    * anything that cannot be read or decided is unknown.

    Through the git seam only; never raises."""
    from code_indexer.global_repos.repointed_origin import (
        CloneSource,
        content_followed_origin,
        read_clone_source,
    )
    from code_indexer.server.storage.shared.git_backend import head_branch
    from code_indexer.services.git_head import head_is_unborn

    try:
        origin_url = str(git.remote_get_url(clone, "origin", timeout=10))
        branch = head_branch(git, clone, timeout=10)
    except Exception as exc:
        return ContentIdentity(
            None, why_unknown=f"git read failed ({type(exc).__name__})"
        )
    origin = parse_git_remote_url(origin_url)
    try:
        source = read_clone_source(git, clone)
    except Exception as exc:
        source = CloneSource(None, f"reading the reflog raised {type(exc).__name__}")
    cloned_from = parse_git_remote_url(source.url) if source.url else None
    if (
        origin is not None
        and cloned_from is not None
        and origin.identity.casefold() == cloned_from.identity.casefold()
    ):
        return ContentIdentity(origin.identity.casefold(), origin_url, branch)
    try:
        unborn = head_is_unborn(git, clone)
    except Exception as exc:
        return ContentIdentity(
            None, why_unknown=f"git read failed ({type(exc).__name__})"
        )
    if unborn:
        # An empty clone has no content to have followed anything: it holds
        # whatever it was cloned from (healed back to it if origin was
        # repointed), else its origin.
        if cloned_from is not None:
            heal = None
            if origin is None or (
                origin.identity.casefold() != cloned_from.identity.casefold()
            ):
                heal = _clean(str(source.url))
            return ContentIdentity(
                cloned_from.identity.casefold(), _clean(str(source.url)), branch, heal
            )
        if origin is not None:
            return ContentIdentity(origin.identity.casefold(), origin_url, branch)
        return ContentIdentity(None, why_unknown="an empty clone with no remote origin")
    try:
        followed = content_followed_origin(git, clone, credentials_url or "")
    except Exception as exc:
        detail = exc if isinstance(exc, ValueError) else type(exc).__name__
        return ContentIdentity(
            None,
            why_unknown=f"whether HEAD followed origin could not be determined ({detail})",
        )
    if followed and origin is not None:
        return ContentIdentity(origin.identity.casefold(), origin_url, branch)
    if cloned_from is None:
        return ContentIdentity(
            None,
            why_unknown=source.unknown_reason
            or "HEAD is not on origin's branch and the clone source is not a remote",
        )
    return ContentIdentity(
        cloned_from.identity.casefold(),
        _clean(str(source.url)),
        branch,
        _clean(str(source.url)),
    )


# -- the plan ---------------------------------------------------------------------


@dataclass
class Rename:
    old: str
    new: str
    repo_url: Optional[str] = None
    branch: Optional[str] = None
    reason: str = ""


@dataclass
class SyncPlan:
    heals: List[Tuple[str, str]] = field(default_factory=list)  # (alias, url)
    renames: List[Rename] = field(default_factory=list)
    adds: List[Tuple[str, CatalogProject]] = field(default_factory=list)
    deferred_adds: List[str] = field(default_factory=list)
    skipped_adds: Dict[str, str] = field(default_factory=dict)  # alias -> why
    vanished: List[Tuple[str, str]] = field(default_factory=list)  # (alias, identity)
    duplicates: Dict[str, List[str]] = field(default_factory=dict)
    conflicts: List[str] = field(default_factory=list)
    unknown: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "heals": [{"alias": a, "origin": u} for a, u in self.heals],
            "renames": [
                {
                    "old": r.old,
                    "new": r.new,
                    "repo_url": r.repo_url,
                    "branch": r.branch,
                    "reason": r.reason,
                }
                for r in self.renames
            ],
            "adds": [{"alias": a, "url": p.url} for a, p in self.adds],
            "deferred_adds": list(self.deferred_adds),
            "skipped_adds": dict(self.skipped_adds),
            "vanished_upstream": [
                {"alias": a, "identity": i} for a, i in self.vanished
            ],
            "duplicates": dict(self.duplicates),
            "conflicts": list(self.conflicts),
            "unknown": dict(self.unknown),
        }


@dataclass(frozen=True)
class Registered:
    """A registered golden repository as the planner sees it."""

    alias: str
    repo_url: str
    content: ContentIdentity


def _under(identity: str, prefixes: Sequence[str]) -> bool:
    return any(identity.startswith(prefix) for prefix in prefixes)


def plan_sync(
    catalog: Dict[str, CatalogProject],
    registered: Sequence[Registered],
    golden_repos_dir: str,
    *,
    managed_prefixes: Sequence[str],
    max_adds: int,
    hold_add: Optional[Callable[[str, CatalogProject], Optional[str]]] = None,
) -> SyncPlan:
    """The heals, renames and adds that make the registry match *catalog*.
    Pure: no I/O. *hold_add* (alias, project) -> why that add must wait, or
    None (:func:`failed_add_holds`)."""
    plan = SyncPlan()
    targets = catalog_aliases(p.path for p in catalog.values())
    target_of = {identity: targets[p.path] for identity, p in catalog.items()}
    by_identity: Dict[str, List[Registered]] = {}
    for row in registered:
        if row.content.identity is None:
            plan.unknown[row.alias] = row.content.why_unknown
            continue
        if row.content.heal_to:
            plan.heals.append((row.alias, row.content.heal_to))
        by_identity.setdefault(row.content.identity, []).append(row)
    taken = {row.alias.casefold() for row in registered}
    keep: Dict[str, str] = {}  # identity -> alias that stays
    wanted: List[Rename] = []
    for identity, rows in sorted(by_identity.items()):
        project = catalog.get(identity)
        if project is None:
            if _under(identity, managed_prefixes):
                plan.vanished.extend((r.alias, identity) for r in rows)
            continue
        target = target_of[identity]
        rows = sorted(
            rows, key=lambda r: (r.alias.casefold() != target.casefold(), r.alias)
        )
        if len(rows) > 1:
            plan.duplicates[identity] = [r.alias for r in rows]
        row = rows[0]
        misassigned = _row_identity(row.repo_url) not in (None, identity)
        legacy = is_legacy_alias(row.alias, project.leaf)
        clashes = any(
            row.alias.casefold() == t.casefold()
            for other, t in target_of.items()
            if other != identity
        )
        if row.alias.casefold() == target.casefold() or not (
            legacy or misassigned or clashes
        ):
            keep[identity] = row.alias
            continue
        url, branch = _corrected(row, project, golden_repos_dir, target, misassigned)
        reason = (
            "misassigned" if misassigned else ("legacy -N" if legacy else "name clash")
        )
        wanted.append(Rename(row.alias, target, url, branch, reason))
    plan.renames = _ordered(wanted, taken, plan.conflicts)
    renamed_to = {r.new.casefold() for r in plan.renames}
    renamed_from = {r.old.casefold() for r in plan.renames}
    occupied = (taken - renamed_from) | renamed_to
    for identity, project in sorted(catalog.items()):
        if identity in by_identity:
            continue
        alias = target_of[identity]
        if alias.casefold() in occupied:
            plan.conflicts.append(
                f"cannot add {project.path} as '{alias}': the name is taken"
            )
            continue
        if project.empty:
            plan.skipped_adds[alias] = EMPTY_PROJECT_REASON
            continue
        held = hold_add(alias, project) if hold_add is not None else None
        if held:
            plan.skipped_adds[alias] = held
            continue
        if len(plan.adds) >= max_adds:
            plan.deferred_adds.append(alias)
            continue
        plan.adds.append((alias, project))
        occupied.add(alias.casefold())
    return plan


def _parse_time(value: Any) -> Optional[datetime]:
    """An ISO time (GitLab's ``Z`` suffix accepted) as an aware datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _job_time(job: Dict[str, Any]) -> datetime:
    """When *job* was submitted (else finished); the epoch when unknown."""
    return (
        _parse_time(job.get("created_at"))
        or _parse_time(job.get("completed_at"))
        or _EPOCH
    )


def failed_add_holds(
    job_tracker: Any, *, now: datetime, retry_after: timedelta
) -> Callable[[str, CatalogProject], Optional[str]]:
    """``hold_add`` for :func:`plan_sync`: an alias whose LAST add job failed
    for the same project is not resubmitted until the project changes upstream
    (GitLab's ``last_activity_at`` after the failure) or *retry_after* has
    passed -- so a permanent failure costs one job per *retry_after*, not one
    per pass, while a transient one is still retried. One job-history read per
    pass; an unreadable history holds nothing (the old behaviour)."""
    latest: Dict[str, Dict[str, Any]] = {}
    if job_tracker is not None:
        try:
            jobs = job_tracker.query_jobs(
                operation_type=ADD_OPERATION, limit=_ADD_HISTORY_LIMIT
            )
        except Exception as exc:
            logger.warning(
                "Golden catalog sync: add job history unreadable (%s); failed "
                "adds are not held back this pass",
                type(exc).__name__,
            )
            jobs = []
        # Newest job per alias decided HERE, by its own timestamp: the
        # history's row order is not part of query_jobs' contract.
        for job in jobs:
            alias = str(job.get("repo_alias") or "")
            if not alias:
                continue
            kept = latest.get(alias)
            if kept is None or _job_time(job) > _job_time(kept):
                latest[alias] = job

    def hold(alias: str, project: CatalogProject) -> Optional[str]:
        job = latest.get(alias)
        if job is None or job.get("status") != "failed":
            return None
        job_url = (job.get("metadata") or {}).get("repo_url")
        if job_url and _row_identity(str(job_url)) != project.identity:
            return None  # a different project's failure under this name
        failed_at = _parse_time(job.get("completed_at") or job.get("created_at"))
        if failed_at is None or now - failed_at >= retry_after:
            return None
        changed = _parse_time(project.last_activity_at)
        if changed is not None and changed > failed_at:
            return None
        error = str(job.get("error") or "unknown error").strip().splitlines()
        return (
            f"its last add (job {job.get('job_id')}) failed at "
            f"{failed_at.isoformat()}: {(error[-1] if error else '')[:200]}; "
            "retried once the project changes upstream or after "
            f"{(failed_at + retry_after).isoformat()}"
        )

    return hold


def _row_identity(repo_url: str) -> Optional[str]:
    parsed = parse_git_remote_url(repo_url)
    return parsed.identity.casefold() if parsed is not None else None


def _corrected(
    row: Registered,
    project: CatalogProject,
    golden_repos_dir: str,
    target: str,
    misassigned: bool,
) -> Tuple[Optional[str], Optional[str]]:
    """The URL/branch a rename records: an in-place (``file://``) row keeps
    its form at the new directory; a misassigned row takes what the clone
    holds (the content's URL and HEAD's branch)."""
    if row.repo_url.startswith("file://"):
        # The checked-out branch is the truth for an in-place clone.
        return f"file://{os.path.join(golden_repos_dir, target)}", row.content.branch
    if misassigned:
        return project.url, row.content.branch
    return None, None


def _ordered(
    wanted: List[Rename], taken: Set[str], conflicts: List[str]
) -> List[Rename]:
    """Renames in an order where each target is free when it runs: a target
    held by another alias waits until that alias has been renamed away. A
    target held by an alias that stays, or a cycle, is a conflict."""
    pending = list(wanted)
    occupied = set(taken)
    done: List[Rename] = []
    while pending:
        ready = [r for r in pending if r.new.casefold() not in occupied]
        if not ready:
            for rename in pending:
                conflicts.append(
                    f"cannot rename '{rename.old}' to '{rename.new}': the name "
                    "is taken (by an alias that stays, or a rename cycle)"
                )
            return done
        for rename in sorted(ready, key=lambda r: r.old):
            occupied.discard(rename.old.casefold())
            occupied.add(rename.new.casefold())
            done.append(rename)
            pending.remove(rename)
    return done


# -- the pass ---------------------------------------------------------------------


@dataclass
class SyncResult:
    mode: str
    plan: Optional[SyncPlan] = None
    applied: Dict[str, Any] = field(default_factory=dict)
    single_flight_skipped: bool = False
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "plan": self.plan.as_dict() if self.plan is not None else None,
            "applied": self.applied,
            "single_flight_skipped": self.single_flight_skipped,
            "error": self.error,
        }


@dataclass
class SyncDeps:
    """What a pass touches; :func:`deps_from_manager` builds it for a server."""

    golden_repos_dir: str
    list_rows: Callable[[], List[Dict[str, Any]]]
    git: Any
    load_catalog: Callable[[], Dict[str, CatalogProject]]
    managed_prefixes: Sequence[str]
    rename_repo: Callable[[Rename], Any]
    add: Callable[[str, CatalogProject], Any]
    set_origin: Callable[[str, str], Any]
    job_tracker: Any = None
    max_adds: int = DEFAULT_MAX_ADDS
    failed_add_retry_seconds: int = DEFAULT_FAILED_ADD_RETRY_SECONDS


def registered_rows(deps: SyncDeps) -> List[Registered]:
    """Every catalog-managed golden repository with its content identity
    (local://, cidx-meta and agent workspaces are not managed here)."""
    rows: List[Registered] = []
    for row in deps.list_rows():
        alias = str(row.get("alias") or "")
        url = str(row.get("repo_url") or "")
        if (
            not alias
            or alias in _UNMANAGED
            or url.startswith("local://")
            or _WORKSPACE_ALIAS.match(alias)
        ):
            continue
        clone = str(row.get("clone_path") or os.path.join(deps.golden_repos_dir, alias))
        credentials = url if parse_git_remote_url(url) is not None else None
        rows.append(
            Registered(alias, url, content_identity(deps.git, clone, credentials))
        )
    return rows


def run_sync(deps: SyncDeps, mode: str) -> SyncResult:
    """One pass in *mode* (``dry-run`` or ``apply``), single-flight across
    the cluster when a job tracker is given."""
    from code_indexer.server.services.job_tracker import DuplicateJobError
    from code_indexer.server.services.single_flight import claim, release

    result = SyncResult(mode)
    if mode not in ("dry-run", "apply"):
        result.error = f"mode {mode!r} runs nothing"
        return result
    try:
        job_id = claim(deps.job_tracker, OPERATION, SENTINEL_ALIAS, SUBMITTER)
    except DuplicateJobError:
        result.single_flight_skipped = True
        return result
    try:
        catalog = deps.load_catalog()
        rows = registered_rows(deps)
        result.plan = plan_sync(
            catalog,
            rows,
            deps.golden_repos_dir,
            managed_prefixes=deps.managed_prefixes,
            max_adds=deps.max_adds,
            hold_add=failed_add_holds(
                deps.job_tracker,
                now=datetime.now(timezone.utc),
                retry_after=timedelta(seconds=deps.failed_add_retry_seconds),
            ),
        )
        if mode == "apply":
            result.applied = apply_plan(deps, result.plan)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        release(deps.job_tracker, job_id, error=result.error)
        logger.error("Golden catalog sync failed: %s", result.error)
        return result
    release(deps.job_tracker, job_id, result=result.as_dict())
    _report(result)
    return result


def apply_plan(deps: SyncDeps, plan: SyncPlan) -> Dict[str, Any]:
    """Heals, then renames in order (each finished before the next), then
    adds. A failed step is recorded; a rename whose target is still held
    because an earlier rename failed is skipped, the others go on."""
    applied: Dict[str, Any] = {
        "healed": [],
        "renamed": [],
        "added": {},
        "failed": {},
        "skipped": {},
    }
    still_held: Set[str] = set()
    for alias, url in plan.heals:
        try:
            deps.set_origin(alias, url)
            applied["healed"].append(alias)
        except Exception as exc:
            applied["failed"][f"heal:{alias}"] = f"{type(exc).__name__}: {exc}"
    for rename in plan.renames:
        if rename.new.casefold() in still_held:
            applied["skipped"][f"rename:{rename.old}"] = (
                f"'{rename.new}' is still held: its rename failed"
            )
            still_held.add(rename.old.casefold())
            continue
        try:
            deps.rename_repo(rename)
            applied["renamed"].append(f"{rename.old}->{rename.new}")
        except Exception as exc:
            applied["failed"][f"rename:{rename.old}"] = f"{type(exc).__name__}: {exc}"
            still_held.add(rename.old.casefold())
    for alias, project in plan.adds:
        try:
            applied["added"][alias] = str(deps.add(alias, project))
        except Exception as exc:
            applied["failed"][f"add:{alias}"] = f"{type(exc).__name__}: {exc}"
    return applied


def _report(result: SyncResult) -> None:
    plan = result.plan
    if plan is None:
        return
    logger.info(
        "Golden catalog sync (%s): %d heal(s), %d rename(s), %d add(s) "
        "(%d deferred, %d skipped), %d vanished upstream (kept), %d conflict(s), "
        "%d unknown",
        result.mode,
        len(plan.heals),
        len(plan.renames),
        len(plan.adds),
        len(plan.deferred_adds),
        len(plan.skipped_adds),
        len(plan.vanished),
        len(plan.conflicts),
        len(plan.unknown),
    )
    for alias, identity in plan.vanished:
        logger.warning(
            "Golden repository '%s' (%s) is no longer in the GitLab catalog; it "
            "is kept -- remove it explicitly if it should go",
            alias,
            identity,
        )


def deps_from_manager(manager: Any) -> SyncDeps:
    """A pass over the running server's golden repositories."""
    from code_indexer.server.services.golden_repo_rename import (
        GoldenRepoRenamer,
        deps_from_manager as rename_deps,
    )

    golden = str(manager.golden_repos_dir)
    groups = _env_list(GROUPS_ENV)
    host = os.environ.get(HOST_ENV, "gitlab.com").strip() or "gitlab.com"

    def catalog() -> Dict[str, CatalogProject]:
        token = gitlab_token() if groups else None
        lister = GitLabGroupLister(host, token) if token else None
        return load_catalog(groups, _env_list(REPOS_ENV), lister)

    def rename(step: Rename) -> Any:
        renamer = GoldenRepoRenamer(rename_deps(manager))
        plan = renamer.plan(step.old, step.new, step.repo_url, step.branch)
        with renamer.locked(plan):
            plan = renamer.plan(step.old, step.new, step.repo_url, step.branch)
            renamer.check_not_busy(plan, own_job=None)
            done = renamer.execute(plan)
        from code_indexer.server.services.golden_repo_audited_ops import (
            record_repo_outcome,
        )

        record_repo_outcome(
            SUBMITTER, "golden_repo_renamed", step.old, "success", new_alias=step.new
        )
        return done

    def add(alias: str, project: CatalogProject) -> Any:
        # No pre-flight ls-remote: the GitLab API listing already proved the
        # project exists, and under git_backend='delegated' the probe goes to
        # git-mcp's git_ls_remote, which returns EVERY ref -- a project with
        # many (front-end/fresca: ~900 refs, ~68 KB) gets a paginated answer
        # the git-mcp backend refuses, and the add failed as "Invalid or
        # inaccessible git repository" every pass (EVO-64616). A real clone
        # failure surfaces in the add job, and failed_add_holds() keeps it
        # from being resubmitted every pass.
        return manager.add_golden_repo(
            repo_url=project.url,
            alias=alias,
            default_branch=project.default_branch,
            submitter_username=SUBMITTER,
            skip_pre_flight_git_validation=True,
        )

    def set_origin(alias: str, url: str) -> Any:
        clone = os.path.join(golden, alias)
        manager._git_backend.remote_set_url(clone, "origin", _clean(url), timeout=10)
        logger.warning(
            "Golden repository '%s': origin restored to its clone source %s "
            "(the content never followed the repointed origin)",
            alias,
            _clean(url),
        )

    return SyncDeps(
        golden_repos_dir=golden,
        list_rows=manager.list_golden_repos,
        git=manager._git_backend,
        load_catalog=catalog,
        managed_prefixes=[f"{host}/{g.strip('/')}/".casefold() for g in groups],
        rename_repo=rename,
        add=add,
        set_origin=set_origin,
        job_tracker=getattr(manager, "job_tracker", None),
        max_adds=positive_int_env(MAX_ADDS_ENV, DEFAULT_MAX_ADDS),
        failed_add_retry_seconds=positive_int_env(
            FAILED_ADD_RETRY_ENV, DEFAULT_FAILED_ADD_RETRY_SECONDS
        ),
    )
