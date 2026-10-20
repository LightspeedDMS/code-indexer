"""Every internal-catalog door, as data, for the Phase 3, 4 and 6 tests.

A *content door* returns catalog content: the restricted caller's COMPLETE
reply must name no repository it cannot see (``cidx_meta_world.leaks``), must
still carry visible catalog content (no vacuous pass) and the admin control
must see the hidden content.  A *refusal door* must refuse the restricted
caller and let the admin through the same gate.  Diagnostics a door is
expected to log are claimed by exact entry (``tests/e2e/log_audit_claims``).
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

from tests.e2e.cidx_meta_world import (
    CATALOG,
    CATALOG_GLOBAL,
    HIDDEN_ASSET,
    RESTRICTED_USER,
    VISIBLE_ASSET,
    CatalogWorld,
    Echo,
    EchoPath,
    Reply,
    leaks,
    mcp,
    mentions,
    rest,
    web_session,
)
from tests.e2e.helpers import wait_for_job
from tests.e2e.log_audit_claims import (
    CLAIM_SETTLE_SECONDS,
    LogClaim,
    LogMark,
    LogWindow,
    claim_expected_entries,
    claim_present_entries,
    exact,
    open_log_mark,
    utc_now,
)
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY

ASYNC_JOB_TIMEOUT_SECONDS = 120.0
ASYNC_JOB_POLL_SECONDS = 0.5
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')

# Exact paths at which a search reply echoes the caller's own query text
# (used only for requests that themselves name the canary).
SEARCH_ECHO_PATHS: Tuple[EchoPath, ...] = (
    ("results", "query_metadata", "query_text"),
    ("query_metadata", "query_text"),
    ("metadata", "query_text"),
    ("query",),
)


def search_echo(*submitted: str) -> Echo:
    """The search echo paths, ignored only where they hold exactly one of
    the *submitted* query or pattern texts."""
    return Echo(SEARCH_ECHO_PATHS, tuple(submitted))


CORRELATION_HEADER = "X-Correlation-ID"
# The wiki renders the fixture's deliberately unparsable "reports" domain
# (its own logging names no file, and rendering may run off the request
# thread); claimed only inside the wiki doors' own call windows.
WIKI_FRONT_MATTER_LOG = exact(
    "code_indexer.server.wiki.wiki_service",
    "Failed to parse front matter, treating as plain content",
    same_request=False,
)
# An auto-watch started on the (non-git) catalog clone logs this off the
# request thread.
GIT_UNAVAILABLE_WATCH_LOG = exact(
    "code_indexer.services.git_aware_watch_handler",
    "Git not available - proceeding with file-only monitoring",
    same_request=False,
)


@dataclass
class Caller:
    """One side of a door call: the restricted user or the admin control."""

    world: CatalogWorld
    restricted: bool
    admin_user: str
    admin_password: str
    _web: Dict[bool, httpx.Client] = field(default_factory=dict)

    def headers(self) -> dict:
        source = self.world.restricted if self.restricted else self.world.admin
        return source.get_headers()

    def _call_id(self) -> Dict[str, str]:
        """A fresh correlation id for one call, recorded for log claims."""
        call_id = f"e2e-catalog-{uuid.uuid4()}"
        self.world.issued_calls.append((utc_now(), call_id))
        return {CORRELATION_HEADER: call_id}

    def token(self) -> str:
        return str(self.headers()["Authorization"]).split(" ", 1)[1]

    def mcp(self, tool: str, args: Dict[str, Any]) -> Reply:
        headers = {**self.headers(), **self._call_id()}
        return mcp(self.world.client, headers, tool, args)

    def rest(self, method: str, path: str, **kwargs: Any) -> Reply:
        headers = {**self.headers(), **kwargs.pop("headers", {}), **self._call_id()}
        return rest(self.world.client, method, path, headers, **kwargs)

    def web(self, method: str, path: str, **kwargs: Any) -> Reply:
        session = self._web.get(self.restricted)
        if session is None:
            user = RESTRICTED_USER if self.restricted else self.admin_user
            password = (
                self.world.restricted_password
                if self.restricted
                else self.admin_password
            )
            session = web_session(self.world, user, password)
            self._web[self.restricted] = session
        headers = {**kwargs.pop("headers", {}), **self._call_id()}
        resp = session.request(
            method, path, follow_redirects=False, headers=headers, **kwargs
        )
        try:
            body: Any = resp.json()
        except ValueError:
            return Reply(resp.status_code, None, resp.text)
        return Reply(resp.status_code, body, resp.text, body, (body,))

    def web_form(self, page: str, path: str, form: Dict[str, str]) -> Reply:
        """POST *form* to *path* with the CSRF token *page* renders; a
        caller the page does not serve gets the page's own answer."""
        shown = self.web("GET", page)
        match = _CSRF.search(shown.text)
        if shown.status != 200 or match is None:
            return shown
        return self.web("POST", path, data={**form, "csrf_token": match.group(1)})

    def log_mark(self) -> LogMark:
        return open_log_mark(self.world.client, self.world.admin_token())

    def _window(self, mark: LogMark) -> LogWindow:
        """The window from *mark* to now, with the ids of its calls."""
        return LogWindow(
            mark.after_id, mark.started, utc_now(), self.world.calls_since(mark.started)
        )

    def claim_logs(self, mark: LogMark, expected: Sequence[LogClaim]) -> None:
        if expected:
            claim_expected_entries(
                self.world.client,
                self.world.admin_token(),
                self._window(mark),
                expected,
            )

    def claim_optional(
        self,
        mark: LogMark,
        claims: Sequence[LogClaim],
        settle_seconds: float = CLAIM_SETTLE_SECONDS,
    ) -> None:
        """Claim *claims* the window's calls logged, waiting *settle_seconds*
        for late writes before the window closes."""
        if not claims:
            return
        time.sleep(settle_seconds)
        claim_present_entries(
            self.world.client, self.world.admin_token(), self._window(mark), claims
        )


DoorCall = Callable[[Caller], Reply]


@dataclass(frozen=True)
class ContentDoor:
    name: str
    call: DoorCall
    visible: str = ALPHA
    admin_sees: str = CANARY
    optional_logs: Tuple[LogClaim, ...] = ()


@dataclass(frozen=True)
class RefusalDoor:
    """``admin_passes`` None: the admin control is not called (see the door).
    ``restricted_logs`` / ``admin_logs``: diagnostics each call must log;
    ``optional_logs``: asynchronous diagnostics claimed if the door's own
    calls logged them."""

    name: str
    call: DoorCall
    refused: Callable[[Reply], bool]
    admin_passes: Optional[Callable[[Reply], bool]]
    restricted_logs: Tuple[LogClaim, ...] = ()
    admin_logs: Tuple[LogClaim, ...] = ()
    optional_logs: Tuple[LogClaim, ...] = ()


def _search(
    query: str, mode: str, alias: Any = CATALOG_GLOBAL, limit: int = 10
) -> DoorCall:
    args = {
        "query_text": query,
        "repository_alias": alias,
        "search_mode": mode,
        "limit": limit,
    }
    return lambda c: c.mcp("search_code", args)


def _rest_query(body: Dict[str, Any]) -> DoorCall:
    return lambda c: c.rest("POST", "/api/query", json=body)


def _async_query_read(read: Callable[[str], str]) -> DoorCall:
    """Submit an async REST query, wait for its job, then read the result
    through *read(job_id)* (the job record or the query-result door)."""

    def call(c: Caller) -> Reply:
        submitted = c.rest(
            "POST",
            "/api/query",
            json={
                "query_text": "ledger payments domain",
                "repository_alias": CATALOG_GLOBAL,
                "limit": 10,
                "async_query": True,
            },
        )
        assert submitted.status == 202, f"async submit: {submitted.snippet()}"
        job_id = submitted.body["job_id"]
        wait_for_job(
            c.world.client,
            job_id,
            c.token(),
            timeout=ASYNC_JOB_TIMEOUT_SECONDS,
            poll_interval=ASYNC_JOB_POLL_SECONDS,
        )
        return c.rest("GET", read(job_id))

    return call


def _web_query(c: Caller) -> Reply:
    return c.web_form(
        "/admin/query",
        "/admin/query",
        {
            "query_text": "ledger payments domain",
            "repository": CATALOG_GLOBAL,
            "search_mode": "semantic",
            "limit": "10",
        },
    )


def _wiki(path: str) -> DoorCall:
    return lambda c: c.rest("GET", f"/wiki/{CATALOG}/{path}")


def _wiki_analytics(c: Caller) -> Reply:
    """Analytics after the caller asked for the hidden article (a view only
    the admin's request can record)."""
    c.rest("GET", f"/wiki/{CATALOG}/{CANARY}")
    return c.mcp("wiki_article_analytics", {"repo_alias": CATALOG})


def _file(path: str) -> DoorCall:
    args = {"repository_alias": CATALOG_GLOBAL, "file_path": path}
    return lambda c: c.mcp("get_file_content", args)


CONTENT_DOORS: Tuple[ContentDoor, ...] = (
    ContentDoor("mcp_search_semantic", _search("ledger payments domain", "semantic")),
    ContentDoor("mcp_search_fts", _search("ledger", "fts")),
    ContentDoor("mcp_search_hybrid", _search("ledger", "hybrid")),
    ContentDoor(
        "mcp_search_omni_list",
        _search("ledger payments", "semantic", [CATALOG_GLOBAL, f"{ALPHA}-global"]),
    ),
    ContentDoor(
        "mcp_regex_single",
        lambda c: c.mcp(
            "regex_search", {"repository_alias": CATALOG_GLOBAL, "pattern": ALPHA}
        ),
    ),
    ContentDoor(
        "mcp_regex_omni",
        lambda c: c.mcp(
            "regex_search",
            {"repository_alias": [CATALOG_GLOBAL, f"{ALPHA}-global"], "pattern": ALPHA},
        ),
    ),
    ContentDoor("mcp_get_file_content_domain", _file("dependency-map/payments.md")),
    ContentDoor("mcp_get_file_content_description", _file(f"{ALPHA}.md")),
    ContentDoor(
        "mcp_list_files",
        lambda c: c.mcp(
            "list_files", {"repository_alias": CATALOG_GLOBAL, "recursive": True}
        ),
        visible=f"{ALPHA}.md",
    ),
    ContentDoor(
        "mcp_browse_directory",
        lambda c: c.mcp(
            "browse_directory", {"repository_alias": CATALOG_GLOBAL, "recursive": True}
        ),
        visible=f"{ALPHA}.md",
    ),
    ContentDoor(
        "mcp_directory_tree",
        lambda c: c.mcp("directory_tree", {"repository_alias": CATALOG_GLOBAL}),
        visible=f"{ALPHA}.md",
    ),
    ContentDoor(
        "mcp_depmap_cross_domain_graph",
        lambda c: c.mcp("depmap_get_cross_domain_graph", {}),
        visible="ledger",
    ),
    ContentDoor(
        "mcp_depmap_domain_summary",
        lambda c: c.mcp("depmap_get_domain_summary", {"domain_name": "payments"}),
    ),
    ContentDoor(
        "mcp_depmap_hub_domains",
        lambda c: c.mcp("depmap_get_hub_domains", {}),
        visible="payments",
    ),
    ContentDoor(
        "mcp_depmap_stale_domains",
        lambda c: c.mcp("depmap_get_stale_domains", {"days_threshold": 0}),
        visible="payments",
    ),
    ContentDoor(
        "mcp_depmap_repo_domains",
        lambda c: c.mcp("depmap_get_repo_domains", {"repo_name": ALPHA}),
        visible="payments",
    ),
    ContentDoor(
        "mcp_depmap_find_consumers",
        lambda c: c.mcp("depmap_find_consumers", {"repo_name": ALPHA}),
    ),
    ContentDoor(
        "rest_query_semantic",
        _rest_query(
            {
                "query_text": "ledger payments domain",
                "repository_alias": CATALOG_GLOBAL,
                "limit": 10,
            }
        ),
    ),
    ContentDoor(
        "rest_query_fts",
        _rest_query(
            {
                "query_text": "ledger",
                "repository_alias": CATALOG_GLOBAL,
                "search_mode": "fts",
                "limit": 10,
            }
        ),
    ),
    ContentDoor(
        "rest_query_hybrid",
        _rest_query(
            {
                "query_text": "ledger",
                "repository_alias": CATALOG_GLOBAL,
                "search_mode": "hybrid",
                "limit": 10,
            }
        ),
    ),
    ContentDoor(
        "rest_query_no_alias",
        _rest_query({"query_text": "ledger payments domain", "limit": 10}),
    ),
    ContentDoor(
        "rest_async_job_record", _async_query_read(lambda job: f"/api/jobs/{job}")
    ),
    ContentDoor(
        "rest_async_query_result",
        _async_query_read(lambda job: f"/api/query/result/{job}"),
    ),
    ContentDoor(
        "rest_query_multi",
        lambda c: c.rest(
            "POST",
            "/api/query/multi",
            json={
                "query": "ledger payments domain",
                "repositories": [CATALOG_GLOBAL, f"{ALPHA}-global"],
                "search_type": "semantic",
                "limit": 10,
            },
        ),
    ),
    ContentDoor(
        "rest_regex",
        lambda c: c.rest(
            "POST",
            "/api/regex/search",
            json={"repository_alias": CATALOG_GLOBAL, "pattern": ALPHA},
        ),
    ),
    ContentDoor(
        "rest_repository_description",
        lambda c: c.rest("GET", f"/api/repositories/{ALPHA}/description"),
    ),
    ContentDoor(
        "web_dependency_map_graph",
        lambda c: c.web("GET", "/admin/dependency-map/graph-data"),
        visible="payments",
    ),
    ContentDoor(
        "web_dependency_map_domain_explorer",
        lambda c: c.web("GET", "/admin/partials/depmap-domain-explorer"),
        visible="payments",
    ),
    ContentDoor(
        "web_dependency_map_domain_detail",
        lambda c: c.web("GET", "/admin/partials/depmap-domain-detail/payments"),
    ),
    *(
        ContentDoor(name, call, visible=visible, optional_logs=(WIKI_FRONT_MATTER_LOG,))
        for name, call, visible in (
            ("wiki_index", _wiki(""), "payments"),
            ("wiki_article_domain", _wiki("dependency-map/payments"), ALPHA),
            ("wiki_article_description", _wiki(ALPHA), ALPHA),
            (
                "wiki_search_semantic",
                _wiki("_search?q=ledger+payments+domain"),
                ALPHA,
            ),
            (
                "wiki_search_fts",
                # The hidden description mentions alpha too: the admin
                # control's result paths include it.
                _wiki(f"_search?q={ALPHA}&mode=fts"),
                "dependency-map/payments",
            ),
            ("mcp_wiki_article_analytics", _wiki_analytics, ALPHA),
        )
    ),
)


def _error_text(reply: Reply) -> str:
    body = reply.body
    if isinstance(body, dict):
        return str(body.get("error") or body.get("detail") or body.get("message") or "")
    return reply.text


def _refused_with(text: str) -> Callable[[Reply], bool]:
    return lambda r: text.lower() in r.text.lower()


def _not_refused_with(text: str) -> Callable[[Reply], bool]:
    return lambda r: r.status < 500 and text.lower() not in r.text.lower()


def _shows_canary(r: Reply) -> bool:
    return r.ok and mentions(r, CANARY)


_HISTORY_REFUSAL = "Git history is not available for this repository"
_XRAY_EVALUATOR = "fn evaluate_node(n: &Node) -> bool { true }"
_TEMPORAL_REFUSAL_LOG = exact(
    "code_indexer.server.mcp.handlers.search",
    "Error in search_code: Temporal search is not available for this repository.",
)
# Logged by the temporal dispatcher off the request thread.
_NO_TEMPORAL_INDEX_LOG = exact(
    "code_indexer.services.temporal.temporal_fusion_dispatch",
    "No temporal indexes available. Run cidx index --index-commits "
    "to create temporal indexes.",
    same_request=False,
)


def _git_door(tool: str, args: Dict[str, Any]) -> RefusalDoor:
    return RefusalDoor(
        f"mcp_{tool}",
        lambda c: c.mcp(tool, {"repository_alias": CATALOG_GLOBAL, **args}),
        refused=_refused_with(_HISTORY_REFUSAL),
        admin_passes=_not_refused_with(_HISTORY_REFUSAL),
    )


def _rest_git_door(route: str) -> RefusalDoor:
    # Admin control omitted: these routes serve activations only, and an
    # admin request for the non-activated catalog alias fails inside the
    # router (not the catalog gate); the MCP history doors carry the admin
    # control for the history rule.
    path = f"/api/v1/repos/{CATALOG_GLOBAL}/git/{route}"
    return RefusalDoor(
        f"rest_git_{route.split('?')[0]}",
        lambda c: c.rest("GET", path),
        refused=lambda r: r.status in (403, 404),
        admin_passes=None,
    )


REFUSAL_DOORS: Tuple[RefusalDoor, ...] = (
    RefusalDoor(
        "mcp_search_temporal",
        lambda c: c.mcp(
            "search_code",
            {
                "query_text": "payments",
                "repository_alias": CATALOG_GLOBAL,
                "time_range_all": True,
            },
        ),
        refused=_refused_with("Temporal search is not available"),
        admin_passes=lambda r: r.ok,
        restricted_logs=(_TEMPORAL_REFUSAL_LOG,),
        admin_logs=(_NO_TEMPORAL_INDEX_LOG,),
    ),
    RefusalDoor(
        "rest_query_temporal",
        _rest_query(
            {
                "query_text": "payments",
                "repository_alias": CATALOG_GLOBAL,
                "time_range_all": True,
            }
        ),
        refused=lambda r: r.status == 400
        and mentions(r, "Temporal search is not available"),
        admin_passes=lambda r: r.status == 200,
    ),
    RefusalDoor(
        "mcp_regex_pcre2",
        lambda c: c.mcp(
            "regex_search",
            {"repository_alias": CATALOG_GLOBAL, "pattern": ALPHA, "pcre2": True},
        ),
        refused=_refused_with("advanced regex mode is not available"),
        admin_passes=_shows_canary,
    ),
    RefusalDoor(
        "rest_regex_pcre2",
        lambda c: c.rest(
            "POST",
            "/api/regex/search",
            json={"repository_alias": CATALOG_GLOBAL, "pattern": ALPHA, "pcre2": True},
        ),
        refused=lambda r: r.status == 422 and mentions(r, "advanced_regex_unavailable"),
        admin_passes=lambda r: r.status == 200 and mentions(r, CANARY),
    ),
    RefusalDoor(
        # The dispatcher authorises a wildcard alias only for admins.
        "mcp_search_wildcard",
        lambda c: c.mcp(
            "search_code",
            {"query_text": "ledger", "repository_alias": "*-global", "limit": 10},
        ),
        refused=_refused_with("is not accessible to user"),
        admin_passes=_shows_canary,
    ),
    _git_door("git_log", {}),
    _git_door("git_file_history", {"path": f"{ALPHA}.md"}),
    _git_door("git_search_commits", {"query": ALPHA}),
    _rest_git_door("log"),
    _rest_git_door(f"file-history?path={ALPHA}.md"),
    _rest_git_door(f"cat?path={ALPHA}.md"),
    RefusalDoor(
        "mcp_xray_search",
        lambda c: c.mcp(
            "xray_search",
            {
                "repository_alias": CATALOG_GLOBAL,
                "pattern": ALPHA,
                "search_target": "content",
                "evaluator_code": _XRAY_EVALUATOR,
            },
        ),
        refused=_refused_with("xray_not_available"),
        admin_passes=_not_refused_with("xray_not_available"),
    ),
    RefusalDoor(
        "rest_xray_search",
        lambda c: c.rest(
            "POST",
            "/api/xray/search",
            json={
                "repository_alias": CATALOG_GLOBAL,
                "driver_regex": ALPHA,
                "evaluator_code": _XRAY_EVALUATOR,
                "search_target": "content",
            },
        ),
        refused=lambda r: r.status == 403 and mentions(r, "xray_not_available"),
        admin_passes=lambda r: r.status == 202,
    ),
    RefusalDoor(
        "mcp_get_file_content_internals",
        _file(".code-indexer-override.yaml"),
        refused=lambda r: not r.ok and mentions(r, "File not found"),
        admin_passes=lambda r: r.ok and mentions(r, "add_exclude_dirs"),
    ),
    RefusalDoor(
        # The Web UI query page serves admin sessions only.
        "web_query_page",
        _web_query,
        refused=lambda r: r.status == 303,
        admin_passes=lambda r: r.status == 200 and mentions(r, CANARY),
    ),
    RefusalDoor(
        "wiki_article_hidden",
        _wiki(CANARY),
        refused=lambda r: r.status == 404,
        admin_passes=lambda r: r.status == 200 and mentions(r, "alphabet of flags"),
        optional_logs=(WIKI_FRONT_MATTER_LOG,),
    ),
    RefusalDoor(
        "wiki_asset_hidden",
        _wiki(f"_assets/{HIDDEN_ASSET}"),
        refused=lambda r: r.status == 404,
        admin_passes=lambda r: r.status == 200 and mentions(r, CANARY),
    ),
    RefusalDoor(
        # A catalog file outside the recognised catalog structures (here an
        # asset named after a visible repository) is denied by default to
        # restricted callers; the admin gets it.
        "wiki_asset_unrecognised",
        _wiki(f"_assets/{VISIBLE_ASSET}"),
        refused=lambda r: r.status == 404,
        admin_passes=lambda r: r.status == 200 and mentions(r, ALPHA),
    ),
)


def assert_content_door(door: ContentDoor, user: Caller, admin: Caller) -> None:
    mark = user.log_mark()
    try:
        _assert_content_replies(door, user, admin)
    finally:
        user.claim_optional(mark, door.optional_logs)


def _assert_content_replies(door: ContentDoor, user: Caller, admin: Caller) -> None:
    restricted = door.call(user)
    assert restricted.status < 400, f"{door.name} restricted: {restricted.snippet()}"
    found = leaks(restricted)
    assert not found, f"{door.name} restricted reply names hidden repos: {found}"
    assert mentions(restricted, door.visible), (
        f"{door.name} restricted reply lacks visible content {door.visible!r}: "
        f"{restricted.snippet()}"
    )
    control = door.call(admin)
    assert control.status < 400, f"{door.name} admin: {control.snippet()}"
    assert mentions(control, door.admin_sees), (
        f"{door.name} admin control lacks {door.admin_sees!r}: {control.snippet()}"
    )


def assert_refusal_door(door: RefusalDoor, user: Caller, admin: Caller) -> None:
    start = user.log_mark()
    try:
        _assert_refusal_replies(door, user, admin)
    finally:
        user.claim_optional(start, door.optional_logs)


def _assert_refusal_replies(door: RefusalDoor, user: Caller, admin: Caller) -> None:
    mark = user.log_mark()
    restricted = door.call(user)
    assert door.refused(restricted), f"{door.name} not refused: {restricted.snippet()}"
    found = leaks(restricted)
    assert not found, f"{door.name} refusal names hidden repos: {found}"
    user.claim_logs(mark, door.restricted_logs)
    if door.admin_passes is not None:
        mark = admin.log_mark()
        control = door.call(admin)
        assert door.admin_passes(control), (
            f"{door.name} admin control: {control.snippet()}"
        )
        admin.claim_logs(mark, door.admin_logs)


def result_paths(reply: Reply) -> List[str]:
    """File paths of a search reply's hits (MCP or REST shapes)."""
    body = reply.body if isinstance(reply.body, dict) else {}
    results = body.get("results")
    if isinstance(results, dict):
        results = results.get("results")
    rows = results if isinstance(results, list) else body.get("fts_results") or []
    return [
        str(r.get("file_path") or r.get("path")) for r in rows if isinstance(r, dict)
    ]


def regex_paths(reply: Reply) -> List[str]:
    body = reply.body if isinstance(reply.body, dict) else {}
    return [
        str(m.get("file_path"))
        for m in body.get("matches") or []
        if isinstance(m, dict)
    ]


def dependency_total(reply: Reply) -> int:
    body = reply.body if isinstance(reply.body, dict) else {}
    return sum(int(e.get("dependency_count", 0)) for e in body.get("edges") or [])


def error_text(reply: Reply) -> str:
    return _error_text(reply)
