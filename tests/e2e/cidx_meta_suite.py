"""The catalog door suite shared by Phases 3, 4 and 6.

A phase's test module subclasses ``CatalogDoorSuite`` and provides a
module-scoped ``catalog_callers`` fixture returning (restricted, admin)
callers on a provisioned world (``cidx_meta_world.provision_catalog_world``)
after ``open_catalog_doors`` (enables the catalog wiki).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Tuple

import pytest

from tests.e2e.cidx_meta_doors import (
    CONTENT_DOORS,
    REFUSAL_DOORS,
    SEARCH_ECHO,
    WIKI_FRONT_MATTER_LOG,
    Caller,
    ContentDoor,
    RefusalDoor,
    assert_content_door,
    assert_refusal_door,
    dependency_total,
    error_text,
    regex_paths,
    result_paths,
)
from tests.e2e.cidx_meta_tool_recipes import RECIPES, repository_tools
from tests.e2e.cidx_meta_world import (
    CATALOG,
    CATALOG_GLOBAL,
    Reply,
    leaks,
    mentions,
)
from tests.e2e.helpers import wait_for_job
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY

Callers = Tuple[Caller, Caller]
JOB_TIMEOUT_SECONDS = 180.0
WRITE_REFUSAL = "This path is not available for writing in this repository."
_DOMAIN_COUNT = re.compile(r"\((\d+) domains\)")
ACTIVATION_LOGS = (
    "Failed to set git config user.email",
    "Failed to set git config user.name",
)
ACTIVATION_WINDOW_LOGS = (
    "Git not available - proceeding with file-only monitoring",
    "Watch thread did not stop within 5 seconds",
    "Repository deactivation initiated",
)
# Longer than the watch manager's 5-second stop wait.
ACTIVATION_SETTLE_SECONDS = 8.0
_SUITE_NAMES = frozenset(
    {d.name for d in CONTENT_DOORS}
    | {d.name for d in REFUSAL_DOORS}
    | {
        "test_catalog_activation_is_refused",
        "test_hidden_catalog_writes_share_one_refusal",
    }
)


def set_catalog_wiki(admin: Caller, enabled: bool) -> None:
    """Enable or disable the catalog wiki through the admin Web toggle."""
    reply = admin.web_form(
        "/admin/golden-repos",
        f"/admin/golden-repos/{CATALOG}/wiki-toggle",
        {"wiki_enabled": "1" if enabled else "0"},
    )
    assert reply.status in (200, 303), f"wiki toggle: {reply.snippet()}"


def open_catalog_doors(callers: Callers) -> None:
    """Enable the catalog wiki; enabling it renders the fixture's articles,
    including the deliberately unparsable "reports" domain."""
    admin = callers[1]
    mark = admin.log_mark()
    set_catalog_wiki(admin, True)
    admin.claim_optional(mark, (WIKI_FRONT_MATTER_LOG,))


def close_catalog_doors(callers: Callers) -> None:
    set_catalog_wiki(callers[1], False)


def _domain_count(reply: Reply) -> int:
    section = (reply.body or {}).get("dependency_map") or ""
    match = _DOMAIN_COUNT.search(str(section))
    assert match, f"quick reference has no dependency map count: {reply.snippet()}"
    return int(match.group(1))


def _wait(caller: Caller, reply: Reply) -> None:
    job = reply.body.get("job_id") if isinstance(reply.body, dict) else None
    if job:
        status = wait_for_job(
            caller.world.client, job, caller.token(), timeout=JOB_TIMEOUT_SECONDS
        )
        assert status.get("status") == "completed", status


def _activated_paths(user: Caller, admin: Caller) -> None:
    """Admin control on an admin activation of the catalog: REST file routes
    and activated search show the hidden content to the admin; the same
    requests from the restricted caller (who cannot activate the catalog)
    name nothing hidden."""
    listing = f"/api/repositories/{CATALOG}/files"
    content = f"{listing}?path={CANARY}.md&content=true"
    for path in (listing, content):
        assert _shows(admin.rest("GET", path)), path
        restricted = user.rest("GET", path)
        assert restricted.status in (403, 404), restricted.snippet()
        assert not leaks(restricted), restricted.snippet()
    search = {
        "query_text": "alphabet",
        "repository_alias": CATALOG,
        "search_mode": "fts",
    }
    regex = {"repository_alias": CATALOG, "pattern": CANARY}
    assert _shows(admin.mcp("search_code", search))
    assert regex_paths(admin.mcp("regex_search", regex))
    # The restricted caller has no activation of that name: the bare alias
    # is served as the global catalog, projected to its view.
    for reply in (user.mcp("search_code", search), user.mcp("regex_search", regex)):
        assert reply.status == 200, reply.snippet()
        assert not leaks(reply, SEARCH_ECHO), reply.snippet()


def _shows(reply: Reply) -> bool:
    return reply.status == 200 and mentions(reply, CANARY)


def _recipe_args(tool: str) -> Dict[str, Any]:
    return dict(RECIPES[tool].args)


CALL_RECIPES = sorted(t for t, r in RECIPES.items() if r.kind == "call")


class CatalogDoorSuite:
    """Every catalog door for one phase (see the module docstring)."""

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

    def test_every_repository_tool_has_a_catalog_recipe(self) -> None:
        selected = repository_tools()
        missing = sorted(selected - set(RECIPES))
        stale = sorted(set(RECIPES) - selected)
        assert not missing, f"repository tools without a catalog recipe: {missing}"
        assert not stale, f"recipes for tools that take no repository: {stale}"
        unknown = sorted(
            r.note
            for r in RECIPES.values()
            if r.kind == "door" and r.note not in _SUITE_NAMES
        )
        assert not unknown, f"recipes naming no door or scenario: {unknown}"
        assert all(r.note for r in RECIPES.values() if r.kind == "exempt")

    @pytest.mark.parametrize("tool", CALL_RECIPES)
    def test_repository_tool_names_nothing_hidden_on_the_catalog(
        self, tool: str, catalog_callers: Callers
    ) -> None:
        user = catalog_callers[0]
        recipe = RECIPES[tool]
        mark = user.log_mark()
        try:
            reply = user.mcp(tool, _recipe_args(tool))
            assert reply.status < 500, reply.snippet()
            assert not leaks(reply), f"{tool}: {leaks(reply)}"
            if not recipe.may_succeed:
                assert not reply.ok, f"{tool} was not refused: {reply.snippet()}"
            user.claim_logs(mark, recipe.logs)
        finally:
            user.claim_optional(mark, recipe.optional_logs)

    def test_hidden_catalog_file_answers_like_a_missing_file(
        self, catalog_callers: Callers
    ) -> None:
        user, admin = catalog_callers

        def read(caller: Caller, path: str) -> Reply:
            return caller.mcp(
                "get_file_content",
                {"repository_alias": CATALOG_GLOBAL, "file_path": path},
            )

        hidden, absent = read(user, f"{CANARY}.md"), read(user, "absent-sample.md")
        assert not hidden.ok and not absent.ok, (hidden.snippet(), absent.snippet())
        assert error_text(hidden) == error_text(absent).replace(
            "absent-sample.md", f"{CANARY}.md"
        )
        assert hidden.body.get("file_content") == absent.body.get("file_content") == []
        control = read(admin, f"{CANARY}.md")
        assert control.ok and mentions(control, "alphabet of flags"), control.snippet()

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
        for reply in (
            user.mcp("search_code", fts),
            user.rest("POST", "/api/query", json=fts),
            user.mcp("regex_search", regex),
            user.rest("POST", "/api/regex/search", json=regex),
        ):
            assert reply.status == 200, reply.snippet()
            assert not result_paths(reply) and not regex_paths(reply), reply.snippet()
            assert not leaks(reply, SEARCH_ECHO), reply.snippet()
        assert result_paths(admin.mcp("search_code", fts))
        assert result_paths(admin.rest("POST", "/api/query", json=fts))
        assert regex_paths(admin.mcp("regex_search", regex))
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
        assert not leaks(reply, SEARCH_ECHO), reply.snippet()
        assert len(result_paths(reply)) == 3, reply.snippet()

    def test_graph_dependency_count_drops_for_restricted_caller(
        self, catalog_callers: Callers
    ) -> None:
        user, admin = catalog_callers
        restricted = dependency_total(user.mcp("depmap_get_cross_domain_graph", {}))
        control = dependency_total(admin.mcp("depmap_get_cross_domain_graph", {}))
        assert 0 < restricted < control, (restricted, control)

    def test_quick_reference_counts_only_visible_domains(
        self, catalog_callers: Callers
    ) -> None:
        user, admin = catalog_callers
        restricted = user.mcp("cidx_quick_reference", {})
        assert not leaks(restricted), restricted.snippet()
        control = admin.mcp("cidx_quick_reference", {})
        assert 0 < _domain_count(restricted) < _domain_count(control)

    def test_poll_search_job_reveals_no_unknown_job(
        self, catalog_callers: Callers
    ) -> None:
        # Search jobs exist only for temporal queries, which the catalog
        # refuses to restricted callers (mcp_search_temporal).
        reply = catalog_callers[0].mcp(
            "poll_search_job", {"job_id": "00000000-0000-4000-8000-000000000000"}
        )
        assert (reply.body or {}).get("status") == "not_found", reply.snippet()
        assert not leaks(reply), reply.snippet()

    def test_catalog_activation_is_refused(self, catalog_callers: Callers) -> None:
        user, admin = catalog_callers
        for reply in (
            user.mcp("activate_repository", {"golden_repo_alias": CATALOG}),
            user.rest(
                "POST", "/api/repos/activate", json={"golden_repo_alias": CATALOG}
            ),
        ):
            assert not reply.ok and mentions(reply, "not accessible"), reply.snippet()
            assert not leaks(reply), reply.snippet()
        mark = admin.log_mark()
        accepted = admin.mcp("activate_repository", {"golden_repo_alias": CATALOG})
        assert accepted.ok, f"admin activation: {accepted.snippet()}"
        _wait(admin, accepted)
        # The catalog is not a git repository: activating it logs these.
        admin.claim_logs(mark, ACTIVATION_LOGS)
        try:
            _activated_paths(user, admin)
        finally:
            removed = admin.mcp("deactivate_repository", {"user_alias": CATALOG})
            assert removed.ok, f"admin deactivation: {removed.snippet()}"
            _wait(admin, removed)
            # Watch start and stop are logged asynchronously; claimed only
            # inside this activation's own window.
            admin.claim_optional(
                mark, ACTIVATION_WINDOW_LOGS, settle_seconds=ACTIVATION_SETTLE_SECONDS
            )

    def test_hidden_catalog_writes_share_one_refusal(
        self, catalog_callers: Callers
    ) -> None:
        user = catalog_callers[0]
        entered = user.mcp("enter_write_mode", {"repo_alias": CATALOG_GLOBAL})
        assert entered.ok, f"enter_write_mode: {entered.snippet()}"
        try:
            mark = user.log_mark()
            replies = [
                user.mcp(
                    "create_file",
                    {
                        "repository_alias": CATALOG_GLOBAL,
                        "file_path": p,
                        "content": "x",
                    },
                )
                for p in (f"{CANARY}.md", f"xray-patterns/{CANARY}/absent.yaml")
            ]
            replies.append(
                user.mcp(
                    "delete_file",
                    {"repository_alias": CATALOG_GLOBAL, "file_path": f"{CANARY}.md"},
                )
            )
            for reply in replies:
                assert error_text(reply) == WRITE_REFUSAL, reply.snippet()
            user.claim_logs(
                mark,
                (
                    f"File create failed - permission denied: {WRITE_REFUSAL}",
                    f"File create failed - permission denied: {WRITE_REFUSAL}",
                    f"File delete failed - permission denied: {WRITE_REFUSAL}",
                ),
            )
        finally:
            mark = user.log_mark()
            exited = user.mcp("exit_write_mode", {"repo_alias": CATALOG_GLOBAL})
            assert exited.ok, f"exit_write_mode: {exited.snippet()}"
            # Leaving write mode stops the catalog clone's auto-watch.
            user.claim_optional(
                mark,
                ("Watch thread did not stop within 5 seconds",),
                settle_seconds=ACTIVATION_SETTLE_SECONDS,
            )

    def test_cached_handle_is_refused_after_grant_revocation(
        self, catalog_callers: Callers
    ) -> None:
        user = catalog_callers[0]
        handle = restricted_catalog_handle(user)
        before = user.mcp("get_cached_content", {"handle": handle})
        assert before.ok and not leaks(before), before.snippet()
        assert user.rest("GET", f"/cache/{handle}").status == 200
        user.world.set_alpha_grant(False)
        try:
            mark = user.log_mark()
            after_mcp = user.mcp("get_cached_content", {"handle": handle})
            after_rest = user.rest("GET", f"/cache/{handle}")
            assert not after_mcp.ok, f"served after revocation: {after_mcp.snippet()}"
            assert after_rest.status == 404, after_rest.snippet()
            user.claim_logs(
                mark,
                (f"[MCP-GENERAL-117] Cache handle not found or expired: {handle}",),
            )
        finally:
            user.world.set_alpha_grant(True)

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
        restricted = user.mcp("search_code", args)
        assert restricted.ok and not leaks(restricted), restricted.snippet()
        assert f"{ALPHA}.md" in result_paths(restricted), restricted.snippet()


def restricted_catalog_handle(user: Caller) -> str:
    """A cache handle from the restricted caller's catalog search."""
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
    return str(handles[0])
