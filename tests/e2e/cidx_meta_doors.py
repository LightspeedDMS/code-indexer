"""Every internal-catalog door, as data, for the Phase 3 and Phase 4 tests.

A *content door* returns catalog content: the restricted caller's full reply
must name no repository it cannot see (``cidx_meta_world.leaks``), must
still carry visible catalog content (no vacuous pass) and the admin control
must see the hidden content.  A *refusal door* must refuse the restricted
caller and let the admin through the same gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest

from tests.e2e.cidx_meta_world import (
    CATALOG,
    CATALOG_GLOBAL,
    RESTRICTED_USER,
    CatalogWorld,
    Reply,
    leaks,
    mcp,
    mentions,
    rest,
    web_session,
)
from tests.e2e.helpers import wait_for_job
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY

ECHO_KEYS = ("query_text", "query", "pattern", "query_metadata")
ASYNC_JOB_TIMEOUT_SECONDS = 120.0
ASYNC_JOB_POLL_SECONDS = 0.5
WRITE_REFUSAL = "This path is not available for writing in this repository."
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


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

    def token(self) -> str:
        return str(self.headers()["Authorization"]).split(" ", 1)[1]

    def mcp(self, tool: str, args: Dict[str, Any]) -> Reply:
        return mcp(self.world.client, self.headers(), tool, args)

    def rest(self, method: str, path: str, **kwargs: Any) -> Reply:
        return rest(self.world.client, method, path, self.headers(), **kwargs)

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
        resp = session.request(method, path, follow_redirects=False, **kwargs)
        try:
            body: Any = resp.json()
        except ValueError:
            body = None
        return Reply(resp.status_code, body, resp.text)


DoorCall = Callable[[Caller], Reply]


@dataclass(frozen=True)
class ContentDoor:
    name: str
    call: DoorCall
    visible: str = ALPHA
    admin_sees: Optional[str] = CANARY


@dataclass(frozen=True)
class RefusalDoor:
    """``admin_passes`` None: the admin control is not called (see the door)."""

    name: str
    call: DoorCall
    refused: Callable[[Reply], bool]
    admin_passes: Optional[Callable[[Reply], bool]]


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
    """The Web UI query page; a caller it does not serve gets the page's
    own answer (a redirect) instead of a form."""
    page = c.web("GET", "/admin/query")
    match = _CSRF.search(page.text)
    if page.status != 200 or match is None:
        return page
    form = {
        "query_text": "ledger payments domain",
        "repository": CATALOG_GLOBAL,
        "search_mode": "semantic",
        "limit": "10",
        "csrf_token": match.group(1),
    }
    return c.web("POST", "/admin/query", data=form)


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
    ContentDoor(
        "mcp_get_file_content_domain",
        lambda c: c.mcp(
            "get_file_content",
            {
                "repository_alias": CATALOG_GLOBAL,
                "file_path": "dependency-map/payments.md",
            },
        ),
    ),
    ContentDoor(
        "mcp_get_file_content_description",
        lambda c: c.mcp(
            "get_file_content",
            {"repository_alias": CATALOG_GLOBAL, "file_path": f"{ALPHA}.md"},
        ),
    ),
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
        # The fixture has no trace repositories and the dependency-map
        # feature flag is off, so the reply carries no catalog content to
        # compare; it must still name nothing hidden.
        "mcp_quick_reference",
        lambda c: c.mcp("cidx_quick_reference", {}),
        visible="tools_by_category",
        admin_sees=None,
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
        "web_dependency_map_domain_detail",
        lambda c: c.web("GET", "/admin/partials/depmap-domain-detail/payments"),
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


_HISTORY_REFUSAL = "Git history is not available for this repository"
_XRAY_EVALUATOR = "fn evaluate_node(n: &Node) -> bool { true }"


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
        admin_passes=lambda r: r.ok,
    ),
    RefusalDoor(
        "rest_regex_pcre2",
        lambda c: c.rest(
            "POST",
            "/api/regex/search",
            json={"repository_alias": CATALOG_GLOBAL, "pattern": ALPHA, "pcre2": True},
        ),
        refused=lambda r: r.status == 422 and mentions(r, "advanced_regex_unavailable"),
        admin_passes=lambda r: r.status == 200,
    ),
    RefusalDoor(
        # The dispatcher authorises a wildcard alias only for admins.
        "mcp_search_wildcard",
        lambda c: c.mcp(
            "search_code",
            {"query_text": "ledger", "repository_alias": "*-global", "limit": 10},
        ),
        refused=_refused_with("is not accessible to user"),
        admin_passes=lambda r: r.ok and mentions(r, CANARY),
    ),
    *(
        RefusalDoor(
            f"mcp_{tool}",
            (
                lambda t, a: (
                    lambda c: c.mcp(t, {"repository_alias": CATALOG_GLOBAL, **a})
                )
            )(tool, args),
            refused=_refused_with(_HISTORY_REFUSAL),
            admin_passes=_not_refused_with(_HISTORY_REFUSAL),
        )
        for tool, args in (
            ("git_log", {}),
            ("git_file_history", {"path": f"{ALPHA}.md"}),
            ("git_search_commits", {"query": ALPHA}),
        )
    ),
    *(
        # Admin control omitted: these routes serve activations only, and an
        # admin request for the non-activated catalog alias fails inside the
        # router (not the catalog gate); the MCP history doors above carry
        # the admin control for the history rule.
        RefusalDoor(
            f"rest_git_{route.split('?')[0]}",
            (lambda p: (lambda c: c.rest("GET", p)))(
                f"/api/v1/repos/{CATALOG_GLOBAL}/git/{route}"
            ),
            refused=lambda r: r.status in (403, 404),
            admin_passes=None,
        )
        for route in ("log", f"file-history?path={ALPHA}.md", f"cat?path={ALPHA}.md")
    ),
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
        lambda c: c.mcp(
            "get_file_content",
            {
                "repository_alias": CATALOG_GLOBAL,
                "file_path": ".code-indexer-override.yaml",
            },
        ),
        refused=lambda r: not r.ok and mentions(r, "File not found"),
        admin_passes=lambda r: r.ok,
    ),
    RefusalDoor(
        # The Web UI query page serves admin sessions only.
        "web_query_page",
        _web_query,
        refused=lambda r: r.status == 303,
        admin_passes=lambda r: r.status == 200 and mentions(r, CANARY),
    ),
)


def assert_content_door(door: ContentDoor, user: Caller, admin: Caller) -> None:
    restricted = door.call(user)
    assert restricted.status < 400, f"{door.name} restricted: {restricted.snippet()}"
    found = leaks(restricted, ECHO_KEYS)
    assert not found, f"{door.name} restricted reply names hidden repos: {found}"
    assert mentions(restricted, door.visible), (
        f"{door.name} restricted reply lacks visible content {door.visible!r}: "
        f"{restricted.snippet()}"
    )
    control = door.call(admin)
    assert control.status < 400, f"{door.name} admin: {control.snippet()}"
    if door.admin_sees is not None:
        assert mentions(control, door.admin_sees), (
            f"{door.name} admin control lacks {door.admin_sees!r}: {control.snippet()}"
        )


def assert_refusal_door(door: RefusalDoor, user: Caller, admin: Caller) -> None:
    restricted = door.call(user)
    assert door.refused(restricted), f"{door.name} not refused: {restricted.snippet()}"
    found = leaks(restricted, ECHO_KEYS)
    assert not found, f"{door.name} refusal names hidden repos: {found}"
    if door.admin_passes is not None:
        control = door.call(admin)
        assert door.admin_passes(control), (
            f"{door.name} admin control: {control.snippet()}"
        )


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


def activate_catalog_refused(user: Caller) -> None:
    """MCP and REST activation of the catalog are refused for the
    restricted caller, naming nothing hidden."""
    for reply in (
        user.mcp("activate_repository", {"golden_repo_alias": CATALOG}),
        user.rest("POST", "/api/repos/activate", json={"golden_repo_alias": CATALOG}),
    ):
        assert not reply.ok and mentions(reply, "not accessible"), reply.snippet()
        assert not leaks(reply), reply.snippet()


def hidden_file_answers_like_missing_file(user: Caller, admin: Caller) -> None:
    """A hidden catalog file is answered exactly like an absent one (the
    reply only echoes the requested path); the admin reads it."""

    def read(caller: Caller, path: str) -> Reply:
        return caller.mcp(
            "get_file_content", {"repository_alias": CATALOG_GLOBAL, "file_path": path}
        )

    hidden, absent = read(user, f"{CANARY}.md"), read(user, "absent-sample.md")
    assert not hidden.ok and not absent.ok, (hidden.snippet(), absent.snippet())
    assert _error_text(hidden) == _error_text(absent).replace(
        "absent-sample.md", f"{CANARY}.md"
    )
    assert hidden.body.get("file_content") == absent.body.get("file_content") == []
    control = read(admin, f"{CANARY}.md")
    assert control.ok and mentions(control, "alphabet of flags"), control.snippet()


def hidden_writes_share_one_refusal(user: Caller) -> None:
    """Writes on hidden catalog paths get one fixed refusal, whether or
    not the path exists."""
    entered = user.mcp("enter_write_mode", {"repo_alias": CATALOG_GLOBAL})
    assert entered.ok, f"enter_write_mode: {entered.snippet()}"
    try:
        replies = [
            user.mcp(
                "create_file",
                {"repository_alias": CATALOG_GLOBAL, "file_path": path, "content": "x"},
            )
            for path in (f"{CANARY}.md", f"xray-patterns/{CANARY}/absent.yaml")
        ]
        replies.append(
            user.mcp(
                "delete_file",
                {"repository_alias": CATALOG_GLOBAL, "file_path": f"{CANARY}.md"},
            )
        )
        for reply in replies:
            assert _error_text(reply) == WRITE_REFUSAL, reply.snippet()
    finally:
        exited = user.mcp("exit_write_mode", {"repo_alias": CATALOG_GLOBAL})
        assert exited.ok, f"exit_write_mode: {exited.snippet()}"


def handle_refused_after_revocation(user: Caller) -> None:
    """A cache handle from the restricted caller's catalog search is served
    while the grants are unchanged and refused (MCP and REST) after one is
    revoked; the grant is restored afterwards."""
    search = user.mcp(
        "search_code",
        {
            "query_text": "domain analysis guidelines cite evidence",
            "repository_alias": CATALOG_GLOBAL,
            "limit": 5,
        },
    )
    rows = (search.body or {}).get("results", {}).get("results", [])
    handles = [r["cache_handle"] for r in rows if r.get("cache_handle")]
    assert handles, (
        f"no cache handle in a restricted catalog search: {search.snippet()}"
    )
    handle = handles[0]
    before = user.mcp("get_cached_content", {"handle": handle})
    assert before.ok and not leaks(before), before.snippet()
    assert user.rest("GET", f"/cache/{handle}").status == 200
    user.world.set_alpha_grant(False)
    try:
        after_mcp = user.mcp("get_cached_content", {"handle": handle})
        after_rest = user.rest("GET", f"/cache/{handle}")
        assert not after_mcp.ok, (
            f"handle served after revocation: {after_mcp.snippet()}"
        )
        assert after_rest.status == 404, (
            f"REST handle after revocation: {after_rest.snippet()}"
        )
    finally:
        user.world.set_alpha_grant(True)


Callers = Tuple[Caller, Caller]


class CatalogDoorSuite:
    """Every catalog door for one phase.  A phase's test module subclasses
    this (``class TestCatalogDoors(CatalogDoorSuite)``) and provides a
    module-scoped ``catalog_callers`` fixture returning (restricted, admin)."""

    # First on purpose: a session-scoped log-audit gate fails in the LAST
    # test's teardown, and pytest reports that as xfailed when the last test
    # is an xfail -- the gate failure would be swallowed.
    @pytest.mark.xfail(
        strict=True,
        reason="a visible catalog hit whose snippet ends at the end of the file is dropped",
    )
    def test_fts_keeps_visible_hit_on_last_line_of_description(
        self, catalog_callers: Callers
    ) -> None:
        user, admin = catalog_callers
        args = {
            "query_text": "ordering",
            "repository_alias": CATALOG_GLOBAL,
            "search_mode": "fts",
        }
        assert f"{ALPHA}.md" in result_paths(admin.mcp("search_code", args))
        assert f"{ALPHA}.md" in result_paths(user.mcp("search_code", args))

    @pytest.mark.parametrize("door", CONTENT_DOORS, ids=lambda d: d.name)
    def test_content_door_shows_only_visible_catalog_content(
        self, door: ContentDoor, catalog_callers: Callers
    ) -> None:
        assert_content_door(door, *catalog_callers)

    @pytest.mark.parametrize("door", REFUSAL_DOORS, ids=lambda d: d.name)
    def test_refusal_door_refuses_restricted_and_admits_admin(
        self, door: RefusalDoor, catalog_callers: Callers
    ) -> None:
        assert_refusal_door(door, *catalog_callers)

    def test_hidden_catalog_file_answers_like_a_missing_file(
        self, catalog_callers: Callers
    ) -> None:
        hidden_file_answers_like_missing_file(*catalog_callers)

    def test_canary_term_finds_nothing_for_restricted_caller(
        self, catalog_callers: Callers
    ) -> None:
        user, admin = catalog_callers
        fts = {
            "query_text": CANARY,
            "repository_alias": CATALOG_GLOBAL,
            "search_mode": "fts",
            "limit": 10,
        }
        regex = {"repository_alias": CATALOG_GLOBAL, "pattern": CANARY}
        assert result_paths(user.mcp("search_code", fts)) == []
        assert result_paths(admin.mcp("search_code", fts))
        assert regex_paths(user.mcp("regex_search", regex)) == []
        assert regex_paths(admin.mcp("regex_search", regex))
        assert regex_paths(user.rest("POST", "/api/regex/search", json=regex)) == []
        assert regex_paths(admin.rest("POST", "/api/regex/search", json=regex))

    def test_hidden_top_hits_still_leave_a_full_page(
        self, catalog_callers: Callers
    ) -> None:
        user, admin = catalog_callers
        args = {
            "query_text": f"{CANARY} settlement worker standalone",
            "repository_alias": CATALOG_GLOBAL,
            "limit": 3,
        }
        top = result_paths(admin.mcp("search_code", args))
        assert any(CANARY in p for p in top), f"admin top hits not hidden ones: {top}"
        reply = user.mcp("search_code", args)
        assert not leaks(reply, ECHO_KEYS), reply.snippet()
        assert len(result_paths(reply)) == 3, reply.snippet()

    def test_graph_dependency_count_drops_for_restricted_caller(
        self, catalog_callers: Callers
    ) -> None:
        user, admin = catalog_callers
        restricted = dependency_total(user.mcp("depmap_get_cross_domain_graph", {}))
        control = dependency_total(admin.mcp("depmap_get_cross_domain_graph", {}))
        assert 0 < restricted < control, (restricted, control)

    def test_catalog_activation_is_refused(self, catalog_callers: Callers) -> None:
        activate_catalog_refused(catalog_callers[0])

    def test_hidden_catalog_writes_share_one_refusal(
        self, catalog_callers: Callers
    ) -> None:
        hidden_writes_share_one_refusal(catalog_callers[0])

    def test_cached_handle_is_refused_after_grant_revocation(
        self, catalog_callers: Callers
    ) -> None:
        handle_refused_after_revocation(catalog_callers[0])
