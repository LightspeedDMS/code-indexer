"""The catalog door suite shared by Phases 3, 4 and 6.

A phase's test module subclasses ``CatalogDoorSuite`` and provides a
module-scoped ``catalog_callers`` fixture returning (restricted, admin)
callers on a provisioned world (``cidx_meta_world.provision_catalog_world``)
after ``open_catalog_doors`` (enables the catalog wiki).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Tuple

import pytest

from tests.e2e.cidx_meta_doors import (
    CONTENT_DOORS,
    REFUSAL_DOORS,
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
    search_echo,
)
from tests.e2e.cidx_meta_tool_recipes import RECIPES, repository_tools
from tests.e2e.cidx_meta_world import (
    CATALOG,
    CATALOG_GLOBAL,
    GIT_HEAD,
    INTERNAL_FILE,
    Reply,
    leaks,
    mentions,
    refresh_in_progress_refusal,
    wait_for_catalog_refresh_after,
    wait_for_catalog_refresh_covering,
    wait_for_idle_catalog_refreshes,
)
from tests.e2e.helpers import wait_for_job
from tests.e2e.log_audit_claims import exact, utc_now
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY

Callers = Tuple[Caller, Caller]
JOB_TIMEOUT_SECONDS = 180.0
WRITE_REFUSAL = "This path is not available for writing in this repository."
ABSENT_FILE = "absent-sample.md"
_DOMAIN_COUNT = re.compile(r"\((\d+) domains\)")
_FILES_LOGGER = "code_indexer.server.mcp.handlers.files"
# Not claimed: the activation job's git-config warnings, "Repository
# deactivation initiated" and the auto-watch start/stop warnings are logged
# off the request thread and their messages name no resource of this test,
# so they cannot be bound to it and are left to the log audit.
_SUITE_NAMES = frozenset(
    {d.name for d in CONTENT_DOORS}
    | {d.name for d in REFUSAL_DOORS}
    | {
        "test_catalog_activation_is_refused",
        "test_hidden_catalog_writes_share_one_refusal",
        "test_quick_reference_counts_only_visible_domains",
        "test_restricted_memory_lifecycle_names_nothing_hidden",
        "test_restricted_xray_pattern_store_names_nothing_hidden",
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


ACTIVATION_ATTEMPTS = 3
_ACTIVATION_LOGGER = "code_indexer.server.repositories.activated_repo_manager"


def _activate_catalog_as_admin(admin: Caller) -> None:
    """The admin's catalog activation, completed.  A catalog refresh running
    when the job starts refuses it with the server's retryable refusal; a
    debounced refresh starts with no job to wait for beforehand, so that
    refusal's two ERROR entries are claimed (bound to the refresh job) and
    the activation is retried once refreshes are idle."""
    for _ in range(ACTIVATION_ATTEMPTS):
        mark = admin.log_mark()
        accepted = admin.mcp("activate_repository", {"golden_repo_alias": CATALOG})
        assert accepted.ok, f"admin activation: {accepted.snippet()}"
        job = str(accepted.body["job_id"])
        status = wait_for_job(
            admin.world.client, job, admin.token(), timeout=JOB_TIMEOUT_SECONDS
        )
        if status.get("status") == "completed":
            return
        error = str(status.get("error") or "")
        refresh = refresh_in_progress_refusal(error)
        assert refresh is not None, status
        admin.claim_logs(
            mark,
            (
                exact(_ACTIVATION_LOGGER, error, bound_to=refresh),
                exact("root", f"Background job {job} failed: {error}", refresh),
            ),
        )
        wait_for_idle_catalog_refreshes(admin.world.client, admin.world.admin)
    raise AssertionError(
        f"admin activation refused by a running catalog refresh "
        f"{ACTIVATION_ATTEMPTS} times"
    )


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
    for reply, submitted in (
        (user.mcp("search_code", search), search["query_text"]),
        (user.mcp("regex_search", regex), regex["pattern"]),
    ):
        assert reply.status == 200, reply.snippet()
        assert not leaks(reply, search_echo(submitted)), reply.snippet()


def _shows(reply: Reply) -> bool:
    return reply.status == 200 and mentions(reply, CANARY)


def _assert_recipe_control(tool: str, reply: Reply, admin: Caller) -> None:
    """A ``may_succeed`` recipe: visible content plus an admin control, or
    the admin control's exact answer where the catalog has nothing to show."""
    recipe = RECIPES[tool]
    assert reply.ok, f"{tool} failed: {reply.snippet()}"
    control = admin.mcp(tool, _recipe_args(tool))
    assert control.ok, f"{tool} admin control: {control.snippet()}"
    if recipe.same_as_admin:
        for answer in (reply, control):
            body = answer.body if isinstance(answer.body, dict) else {}
            got = {key: body.get(key) for key in recipe.expect}
            assert got == recipe.expect, f"{tool}: {answer.snippet()}"
        assert reply.items == control.items, (reply.snippet(), control.snippet())
        return
    assert recipe.shows and mentions(reply, recipe.shows), (
        f"{tool} lacks visible {recipe.shows!r}: {reply.snippet()}"
    )
    assert mentions(control, recipe.admin_shows), (
        f"{tool} admin control lacks {recipe.admin_shows!r}: {control.snippet()}"
    )


def _read_catalog_file(caller: Caller, path: str) -> Reply:
    return caller.mcp(
        "get_file_content", {"repository_alias": CATALOG_GLOBAL, "file_path": path}
    )


def _assert_missing_file_answer(reply: Reply, path: str, caller: Caller) -> None:
    """*reply* for *path* is exactly what *caller* gets for an absent file."""
    absent = _read_catalog_file(caller, ABSENT_FILE)
    assert not reply.ok and not absent.ok, (reply.snippet(), absent.snippet())
    assert error_text(reply) == error_text(absent).replace(ABSENT_FILE, path)
    assert reply.body.get("file_content") == absent.body.get("file_content") == []


def _assert_reads_like_missing_file(caller: Caller, path: str) -> None:
    _assert_missing_file_answer(_read_catalog_file(caller, path), path, caller)


MEMORY_SUMMARY = "Sample catalog visibility memory: alpha entry returns one."
UNKNOWN_REPO = "absent-sample-repo"
PATTERN_YAML = (
    "name: e2e-catalog-sample-pattern\n"
    "description: Sample pattern stored by the catalog visibility suite.\n"
    "language: python\n"
    "evaluator_code: |\n"
    "  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }\n"
)


def _memory_args(summary: str) -> Dict[str, Any]:
    return {
        "type": "gotcha",
        "scope": "repo",
        "scope_target": ALPHA,
        "referenced_repo": ALPHA,
        "summary": summary,
        "evidence": [{"file": f"{ALPHA}_core.py", "lines": "1-4"}],
    }


def _store_pattern(caller: Caller, scope: str) -> Reply:
    return caller.mcp(
        "store_xray_pattern",
        {"scope": scope, "pattern_yaml": PATTERN_YAML, "overwrite": True},
    )


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
        vacuous = sorted(
            tool
            for tool, r in RECIPES.items()
            if r.may_succeed and not (r.shows or r.same_as_admin)
        )
        assert not vacuous, f"may_succeed recipes without a control: {vacuous}"

    @pytest.mark.parametrize("tool", CALL_RECIPES)
    def test_repository_tool_names_nothing_hidden_on_the_catalog(
        self, tool: str, catalog_callers: Callers
    ) -> None:
        user, admin = catalog_callers
        recipe = RECIPES[tool]
        mark = user.log_mark()
        try:
            reply = user.mcp(tool, _recipe_args(tool))
            assert reply.status < 500, reply.snippet()
            assert not leaks(reply), f"{tool}: {leaks(reply)}"
            if not recipe.may_succeed:
                assert not reply.ok, f"{tool} was not refused: {reply.snippet()}"
            else:
                _assert_recipe_control(tool, reply, admin)
            user.claim_logs(mark, recipe.logs)
        finally:
            user.claim_optional(mark, recipe.optional_logs)

    def test_hidden_catalog_file_answers_like_a_missing_file(
        self, catalog_callers: Callers
    ) -> None:
        user, admin = catalog_callers
        _assert_reads_like_missing_file(user, f"{CANARY}.md")
        control = _read_catalog_file(admin, f"{CANARY}.md")
        assert control.ok and mentions(control, "alphabet of flags"), control.snippet()

    def test_internal_metadata_answers_like_a_missing_file(
        self, catalog_callers: Callers
    ) -> None:
        # INTERNAL_FILE really exists in the indexed catalog and the admin
        # control reads it; the restricted caller gets exactly the
        # missing-file answer.  The catalog holds no .git (a .git with only
        # HEAD makes the server treat it as a git repository and breaks
        # activation), so .git/HEAD is checked for the restricted caller
        # only.
        user, admin = catalog_callers
        on_disk = user.world.catalog_root / INTERNAL_FILE
        assert on_disk.is_file(), INTERNAL_FILE
        key = sorted(json.loads(on_disk.read_text(encoding="utf-8")))[0]
        control = _read_catalog_file(admin, INTERNAL_FILE)
        assert control.ok and mentions(control, key), control.snippet()
        _assert_reads_like_missing_file(user, INTERNAL_FILE)
        _assert_reads_like_missing_file(user, GIT_HEAD)

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
            assert not leaks(reply, search_echo(CANARY)), reply.snippet()
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
        assert not leaks(reply, search_echo(str(args["query_text"]))), reply.snippet()
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
        # No search job can carry catalog results: search jobs exist only
        # for temporal queries, and the catalog refuses temporal search
        # (mcp_search_temporal).  What remains to pin is that an unknown job
        # id answers not_found and names nothing.
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
        _activate_catalog_as_admin(admin)
        try:
            _activated_paths(user, admin)
        finally:
            removed = admin.mcp("deactivate_repository", {"user_alias": CATALOG})
            assert removed.ok, f"admin deactivation: {removed.snippet()}"
            _wait(admin, removed)

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
            create = exact(
                _FILES_LOGGER,
                f"[MCP-GENERAL-043] File create failed - permission denied: "
                f"{WRITE_REFUSAL}",
            )
            delete = exact(
                _FILES_LOGGER,
                f"[MCP-GENERAL-055] File delete failed - permission denied: "
                f"{WRITE_REFUSAL}",
            )
            user.claim_logs(mark, (create, create, delete))
        finally:
            exited = user.mcp("exit_write_mode", {"repo_alias": CATALOG_GLOBAL})
            assert exited.ok, f"exit_write_mode: {exited.snippet()}"

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
                (
                    exact(
                        "code_indexer.server.mcp.handlers.search",
                        f"[MCP-GENERAL-117] Cache handle not found or expired: {handle}",
                    ),
                ),
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

    # The two write scenarios run last: a catalog write starts a catalog
    # refresh, during which activating the catalog is refused.
    def test_restricted_memory_lifecycle_names_nothing_hidden(
        self, catalog_callers: Callers
    ) -> None:
        user = catalog_callers[0]
        created = user.mcp("create_memory", _memory_args(MEMORY_SUMMARY))
        assert created.ok and not leaks(created), created.snippet()
        memory_id = str(created.body["id"])
        content_hash = str(created.body["content_hash"])
        try:
            edited = user.mcp(
                "edit_memory",
                {
                    "memory_id": memory_id,
                    "expected_content_hash": content_hash,
                    **_memory_args(f"{MEMORY_SUMMARY} (edited)"),
                },
            )
            if edited.ok:
                content_hash = str(edited.body["content_hash"])
            assert edited.ok and not leaks(edited), edited.snippet()
        finally:
            # The lifecycle leaves the catalog as published: the refresh
            # after the delete covers it and publishes nothing.
            deleting = utc_now()
            deleted = user.mcp(
                "delete_memory",
                {"memory_id": memory_id, "expected_content_hash": content_hash},
            )
            wait_for_catalog_refresh_covering(
                user.world.client, user.world.admin, deleting
            )
            assert deleted.ok and not leaks(deleted), deleted.snippet()

    def test_restricted_xray_pattern_store_names_nothing_hidden(
        self, catalog_callers: Callers
    ) -> None:
        # No delete door exists for patterns: the stored file lives in the
        # catalog and the world teardown's catalog restore removes it.
        user = catalog_callers[0]
        started = utc_now()
        stored = _store_pattern(user, f"{ALPHA}-global")
        hidden = _store_pattern(user, f"{CANARY}-global")
        unknown = _store_pattern(user, f"{UNKNOWN_REPO}-global")
        wait_for_catalog_refresh_after(user.world.client, user.world.admin, started)
        assert stored.ok and not leaks(stored), stored.snippet()
        assert not hidden.ok and not unknown.ok, (hidden.snippet(), unknown.snippet())
        assert hidden.text.replace(CANARY, UNKNOWN_REPO) == unknown.text


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
