"""MCP file doors serve the catalog as the caller may see it.

For a restricted caller every catalog path is decided by the path policy
before anything is read: a denied path is answered exactly like a missing
file, dependency-map documents and descriptions are served projected (line
aligned, so offset/limit address projected lines), and internals
(``.code-indexer/``, ``.git``, root dot-files) are never served. Admins get
the files unchanged. Real catalog on disk, real FileListingService, real
registry and group store. Neutral names only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.server.mcp.handlers import files as files_module
from code_indexer.server.services.cidx_meta_access import RESTRICTED_PLACEHOLDER
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    MEMORY_ALPHA,
    MEMORY_CANARY,
    MEMORY_GLOBAL,
    payments_lines,
)

from .catalog_file_door_env import (
    ALPHA_ALIAS,
    CATALOG_ALIAS,
    CatalogFileDoorEnv,
    mcp_data,
    point_alias,
    user,
)

# A path the policy allows, so its answer is the reader's genuine not-found.
MISSING = "xray-patterns/__any__/missing.yaml"

DENIED = [
    f"{CANARY}.md",
    f"{GAMMA}.md",
    "README.md",
    f"dependency-map/{CANARY}.md",
    "dependency-map/inventory.md",
    "dependency-map/legacy.md",
    "dependency-map/_domains.json",
    "dependency-map/_activity.md",
    "dependency-map/_journal.json",
    "dependency-map/x.jsonl",
    ".code-indexer/index/meta.json",
    ".git/config",
    ".gitignore",
    ".self-check-probe",
    f"xray-patterns/{CANARY}/p.yaml",
    f"memories/{MEMORY_CANARY}.md",
]


@pytest.fixture
def env(tmp_path: Path) -> CatalogFileDoorEnv:
    return CatalogFileDoorEnv(tmp_path)


def _read(
    env: CatalogFileDoorEnv, alias: str, path: str, who: str = "", **extra: Any
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"repository_alias": alias, "file_path": path, **extra}
    with env.serving():
        result = files_module.get_file_content(params, user(who) if who else user())
    return mcp_data(result)


def _text(data: Dict[str, Any]) -> str:
    return "".join(block["text"] for block in data.get("file_content", []))


def _without(data: Dict[str, Any], echo: str) -> str:
    return json.dumps(data).replace(echo, "<path>")


@pytest.mark.parametrize("hidden", DENIED)
def test_denied_catalog_path_reads_exactly_like_a_missing_file(
    env: CatalogFileDoorEnv, hidden: str
) -> None:
    denied = _read(env, CATALOG_ALIAS, hidden)
    missing = _read(env, CATALOG_ALIAS, MISSING)

    assert missing["success"] is False
    assert _without(denied, hidden) == _without(missing, MISSING)


def test_dependency_map_document_is_served_projected_with_its_line_count(
    env: CatalogFileDoorEnv,
) -> None:
    data = _read(env, CATALOG_ALIAS, "dependency-map/payments.md")

    assert data["success"] is True, data
    text = _text(data)
    serialized = _without(data, "dependency-map/payments.md")
    assert CANARY not in serialized and GAMMA not in serialized
    assert ALPHA in text
    assert data["metadata"]["total_lines"] == len(payments_lines())


def test_offset_and_limit_address_projected_lines(env: CatalogFileDoorEnv) -> None:
    whole = _text(_read(env, CATALOG_ALIAS, "dependency-map/payments.md")).split("\n")
    page = _read(env, CATALOG_ALIAS, "dependency-map/payments.md", offset=10, limit=5)

    assert page["success"] is True, page
    assert _text(page).split("\n")[:5] == whole[9:14]
    assert page["metadata"]["offset"] == 10


def test_description_is_served_with_hidden_aliases_redacted(
    env: CatalogFileDoorEnv,
) -> None:
    data = _read(env, CATALOG_ALIAS, f"{ALPHA}.md")

    assert data["success"] is True, data
    text = _text(data)
    assert RESTRICTED_PLACEHOLDER in text
    assert CANARY not in json.dumps(data) and GAMMA not in json.dumps(data)
    assert text.startswith(f"# {ALPHA}")


def test_admin_reads_catalog_files_unchanged(env: CatalogFileDoorEnv) -> None:
    raw = (env.fx.root / "dependency-map" / "payments.md").read_text()
    payments = _read(env, CATALOG_ALIAS, "dependency-map/payments.md", ADMIN_USER)
    canary = _read(env, CATALOG_ALIAS, f"{CANARY}.md", ADMIN_USER)

    assert _text(payments) == raw
    assert canary["success"] is True and CANARY in _text(canary)


VISIBLE = {
    f"{ALPHA}.md",
    "dependency-map/_index.md",
    "dependency-map/payments.md",
    "dependency-map/ledger.md",
    "dependency-map/_dep_types.md",
    "dependency-map/_analysis_guidelines.md",
    "xray-patterns/__any__/p.yaml",
    f"memories/{MEMORY_GLOBAL}.md",
    f"memories/{MEMORY_ALPHA}.md",
}


def _call(
    env: CatalogFileDoorEnv, tool: Any, params: Dict[str, Any], who: str = ""
) -> Dict[str, Any]:
    with env.serving():
        return mcp_data(tool(params, user(who) if who else user()))


def _no_hidden_names(data: Dict[str, Any]) -> None:
    serialized = json.dumps(data)
    assert CANARY not in serialized and f"{GAMMA}." not in serialized


def test_list_files_shows_only_visible_catalog_paths(env: CatalogFileDoorEnv) -> None:
    data = _call(env, files_module.list_files, {"repository_alias": CATALOG_ALIAS})

    assert data["success"] is True, data
    assert {f["path"] for f in data["files"]} == VISIBLE
    _no_hidden_names(data)


def test_browse_directory_shows_only_visible_catalog_paths(
    env: CatalogFileDoorEnv,
) -> None:
    for recursive in (True, False):
        data = _call(
            env,
            files_module.browse_directory,
            {"repository_alias": CATALOG_ALIAS, "recursive": recursive},
        )
        paths = {f["path"] for f in data["structure"]["files"]}
        expected = VISIBLE if recursive else {p for p in VISIBLE if "/" not in p}
        assert paths == expected, recursive
        assert data["structure"]["total"] == len(expected)
        _no_hidden_names(data)


def test_omni_list_files_restricts_the_catalog_entry(env: CatalogFileDoorEnv) -> None:
    data = _call(
        env,
        files_module.list_files,
        {"repository_alias": [CATALOG_ALIAS, ALPHA_ALIAS]},
    )

    catalog = {f["path"] for f in data["files"] if f["source_repo"] == CATALOG_ALIAS}
    assert catalog == VISIBLE
    assert "src/main.py" in {f["path"] for f in data["files"]}
    _no_hidden_names(data)


def test_directory_tree_lists_visible_dependency_map_documents(
    env: CatalogFileDoorEnv,
) -> None:
    data = _call(
        env,
        files_module.handle_directory_tree,
        {"repository_alias": CATALOG_ALIAS, "max_depth": 5},
    )

    assert data["success"] is True, data
    assert "payments.md" in data["tree_string"] and "_index.md" in data["tree_string"]
    assert data["total_files"] == len(VISIBLE)
    _no_hidden_names(data)


def test_directory_tree_of_an_alias_pointer_serving_the_catalog_is_restricted(
    env: CatalogFileDoorEnv,
) -> None:
    point_alias(env.golden_root, ALPHA_ALIAS, env.fx.root)

    data = _call(
        env,
        files_module.handle_directory_tree,
        {"repository_alias": ALPHA_ALIAS, "max_depth": 5},
    )

    assert data["success"] is True, data
    assert data["total_files"] == len(VISIBLE)
    _no_hidden_names(data)


def test_catalog_copy_under_a_custom_alias_is_restricted(
    env: CatalogFileDoorEnv, tmp_path: Path
) -> None:
    env.catalog_copy(tmp_path)

    listed = _call(env, files_module.list_files, {"repository_alias": "team-notes"})
    payments = _read(env, "team-notes", "dependency-map/payments.md")
    hidden = _read(env, "team-notes", f"{CANARY}.md")

    assert {f["path"] for f in listed["files"]} == VISIBLE
    assert payments["success"] is True and ALPHA in _text(payments)
    _no_hidden_names(listed)
    _no_hidden_names(payments)
    assert hidden["success"] is False and _text(hidden) == ""


def test_bare_alias_recovery_to_the_catalog_is_restricted(
    env: CatalogFileDoorEnv, tmp_path: Path
) -> None:
    env.activations.add("cidx-meta", "cidx-meta", tmp_path / "removed-activation")

    denied = _read(env, "cidx-meta", f"{CANARY}.md")
    shown = _read(env, "cidx-meta", f"{ALPHA}.md")

    assert denied["success"] is False and _text(denied) == ""
    assert shown["success"] is True and RESTRICTED_PLACEHOLDER in _text(shown)
    _no_hidden_names(shown)


def test_catalog_without_access_service_shows_nothing(env: CatalogFileDoorEnv) -> None:
    env.state.access_filtering_service = None

    listed = _call(env, files_module.list_files, {"repository_alias": CATALOG_ALIAS})
    read = _read(env, CATALOG_ALIAS, f"{ALPHA}.md")

    assert listed["success"] is True and listed["files"] == []
    assert read["success"] is False and _text(read) == ""


def test_ordinary_repository_is_not_filtered(env: CatalogFileDoorEnv) -> None:
    listed = _call(env, files_module.list_files, {"repository_alias": ALPHA_ALIAS})
    read = _read(env, ALPHA_ALIAS, "src/main.py")

    assert [f["path"] for f in listed["files"]] == ["src/main.py"]
    assert _text(read) == "print('alpha')\n"


def test_admin_listing_is_unchanged(env: CatalogFileDoorEnv) -> None:
    data = _call(
        env, files_module.list_files, {"repository_alias": CATALOG_ALIAS}, ADMIN_USER
    )

    paths = {f["path"] for f in data["files"]}
    assert f"{CANARY}.md" in paths and "dependency-map/_domains.json" in paths


def _alias_and_path_tools() -> Dict[str, Dict[str, Any]]:
    from code_indexer.server.mcp.tools import TOOL_REGISTRY

    found = {}
    for name, tool in TOOL_REGISTRY.items():
        props = (tool.get("inputSchema") or {}).get("properties", {})
        if "repository_alias" in props and props.keys() & {"file_path", "path"}:
            found[name] = props
    return found


# File doors this test drives against the catalog. Every other tool taking an
# alias plus a path is answered by another policy and must be named here, so a
# new file tool cannot appear unclassified.
FILE_DOOR_TOOLS = {
    "get_file_content",
    "list_files",
    "browse_directory",
    "directory_tree",
}
OTHER_POLICY_TOOLS = {
    # write tools: catalog writes need write mode and repository:write
    "create_file",
    "edit_file",
    "delete_file",
    "git_checkout_file",
    "git_mark_resolved",
    "comment_on_pull_request",
    # history policy (admin-only on the catalog)
    "git_blame",
    "git_diff",
    "git_file_at_revision",
    "git_file_history",
    "git_log",
    "git_search_diffs",
    # X-Ray policy (admin-only on the catalog)
    "xray_dump_ast",
    "xray_explore",
    "xray_search",
    # search and regex doors
    "regex_search",
}


def test_every_alias_and_path_tool_is_classified() -> None:
    assert set(_alias_and_path_tools()) == FILE_DOOR_TOOLS | OTHER_POLICY_TOOLS


@pytest.mark.parametrize("tool_name", sorted(FILE_DOOR_TOOLS))
@pytest.mark.parametrize("path", ["", f"{CANARY}.md", "dependency-map"])
def test_file_door_tool_on_the_catalog_never_shows_hidden_names(
    env: CatalogFileDoorEnv, tool_name: str, path: str
) -> None:
    from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

    props = _alias_and_path_tools()[tool_name]
    params: Dict[str, Any] = {"repository_alias": CATALOG_ALIAS}
    params["file_path" if "file_path" in props else "path"] = path or f"{ALPHA}.md"
    data = _call(env, HANDLER_REGISTRY[tool_name], params)

    _no_hidden_names(json.loads(_without(data, path) if path else json.dumps(data)))


def test_listings_of_an_alias_pointer_serving_the_catalog_are_restricted(
    env: CatalogFileDoorEnv,
) -> None:
    point_alias(env.golden_root, ALPHA_ALIAS, env.fx.root)

    listed = _call(env, files_module.list_files, {"repository_alias": ALPHA_ALIAS})
    browsed = _call(
        env, files_module.browse_directory, {"repository_alias": ALPHA_ALIAS}
    )
    omni = _call(env, files_module.list_files, {"repository_alias": [ALPHA_ALIAS]})

    assert {f["path"] for f in listed["files"]} == VISIBLE
    assert {f["path"] for f in browsed["structure"]["files"]} == VISIBLE
    assert {f["path"] for f in omni["files"]} == VISIBLE
    for data in (listed, browsed, omni):
        _no_hidden_names(data)


def test_alias_pointer_serving_the_catalog_is_restricted(
    env: CatalogFileDoorEnv,
) -> None:
    point_alias(env.golden_root, ALPHA_ALIAS, env.fx.root)

    denied = _read(env, ALPHA_ALIAS, f"{CANARY}.md")
    missing = _read(env, ALPHA_ALIAS, MISSING)

    assert _without(denied, f"{CANARY}.md") == _without(missing, MISSING)
