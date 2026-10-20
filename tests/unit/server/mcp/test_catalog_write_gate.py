"""MCP file write tools judge the canonical catalog path before writing.

For a restricted caller every write on the catalog (the write-exception
alias in write mode, or a pre-existing activated copy) decides the path with
the catalog path policy on its canonical form BEFORE any existence check or
read: a path the caller may not see answers ONE fixed refusal, identical
whether or not the file exists, and nothing on disk changes. Restricted
callers may write only catalog files they see unchanged: descriptions and
dependency-map documents get the same refusal, so no write reply depends on
text projected away. Admins keep today's behaviour. A lock-in classifies every write tool
taking a path, so a new one cannot skip a gate. Real catalog on disk, real
FileCRUDService, registry and group store. Neutral names only.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.mcp.handlers import files as files_module
from code_indexer.server.services import file_crud_service as crud_module
from code_indexer.server.services.cidx_meta_access.history_policy import (
    CatalogSurface,
)
from code_indexer.server.services.file_crud_service import file_crud_service
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    MEMORY_ALPHA,
    MEMORY_CANARY,
)

from .catalog_file_door_env import (
    CATALOG_ALIAS,
    CATALOG_COPY_ALIAS,
    CatalogFileDoorEnv,
    mcp_data,
    user,
)

REFUSAL = "This path is not available for writing in this repository."

# (existing, missing) pairs of paths a restricted caller may not see.
HIDDEN = [
    (f"{CANARY}.md", f"{CANARY}-other.md"),
    (f"dependency-map/{CANARY}.md", "dependency-map/hidden-domain.md"),
    (f"xray-patterns/{CANARY}/p.yaml", f"xray-patterns/{CANARY}/q.yaml"),
    (".gitignore", ".hidden-new"),
    (f"memories/{MEMORY_CANARY}.md", f"memories/{'d4' * 16}.md"),
    ("xray-patterns/__any__/hidden-link/p.yaml", "xray-patterns/__any__/hidden-link/q"),
]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[CatalogFileDoorEnv]:
    """The catalog registered as a write exception in write mode; the
    singleton's registrations are restored afterwards."""
    env = CatalogFileDoorEnv(tmp_path)
    (env.fx.root / "xray-patterns" / "__any__" / "hidden-link").symlink_to(
        env.fx.root / "xray-patterns" / CANARY, target_is_directory=True
    )
    marker = env.golden_root / ".write_mode" / "cidx-meta.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}")
    saved = dict(file_crud_service._global_write_exceptions)
    saved_golden = file_crud_service._golden_repos_dir
    file_crud_service.register_write_exception(CATALOG_ALIAS, env.fx.root)
    file_crud_service.set_golden_repos_dir(env.golden_root)
    try:
        with patch.object(
            crud_module, "_get_activated_repo_manager", lambda: env.activations
        ):
            yield env
    finally:
        file_crud_service._global_write_exceptions = saved
        file_crud_service._golden_repos_dir = saved_golden


ToolCall = Tuple[Callable[..., Dict[str, Any]], Dict[str, Any]]
TOOLS: Dict[str, ToolCall] = {
    "create_file": (files_module.handle_create_file, {"content": "probe\n"}),
    "edit_file": (
        files_module.handle_edit_file,
        {"old_string": "a", "new_string": "b", "content_hash": "0" * 64},
    ),
    "delete_file": (files_module.handle_delete_file, {}),
}


def _write(
    env: CatalogFileDoorEnv, tool: str, alias: str, path: str, who: str = ""
) -> Dict[str, Any]:
    handler, extra = TOOLS[tool]
    params = {"repository_alias": alias, "file_path": path, **extra}
    with env.serving():
        return mcp_data(handler(params, user(who) if who else user()))


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


@pytest.mark.parametrize("tool", sorted(TOOLS))
@pytest.mark.parametrize("existing,missing", HIDDEN)
def test_hidden_catalog_path_gets_one_refusal_whether_or_not_it_exists(
    env: CatalogFileDoorEnv, tool: str, existing: str, missing: str
) -> None:
    before = _snapshot(env.fx.root)

    for path in (existing, missing):
        assert _write(env, tool, CATALOG_ALIAS, path) == {
            "success": False,
            "error": REFUSAL,
        }, path
    assert _snapshot(env.fx.root) == before


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_activated_catalog_copy_refuses_hidden_paths(
    env: CatalogFileDoorEnv, tmp_path: Path, tool: str
) -> None:
    copy = env.catalog_copy(tmp_path)
    before = _snapshot(copy)

    for path in (f"{CANARY}.md", f"{CANARY}-other.md"):
        data = _write(env, tool, CATALOG_COPY_ALIAS, path)
        assert data == {"success": False, "error": REFUSAL}, (path, data)
    assert _snapshot(copy) == before


# Paths a restricted caller sees only projected: a description of a visible
# repository (existing or not) and visible dependency-map documents.
PROJECTED = [
    f"{ALPHA}.md",
    "dependency-map/payments.md",
    "dependency-map/_index.md",
]


@pytest.mark.parametrize("tool", sorted(TOOLS))
@pytest.mark.parametrize("path", PROJECTED)
def test_projected_catalog_paths_get_the_fixed_refusal(
    env: CatalogFileDoorEnv, tool: str, path: str
) -> None:
    before = _snapshot(env.fx.root)

    data = _write(env, tool, CATALOG_ALIAS, path)

    assert data == {"success": False, "error": REFUSAL}, (path, data)
    assert _snapshot(env.fx.root) == before


def test_missing_description_of_a_visible_repository_is_refused(
    env: CatalogFileDoorEnv,
) -> None:
    (env.fx.root / f"{ALPHA}.md").unlink(missing_ok=True)

    data = _write(env, "create_file", CATALOG_ALIAS, f"{ALPHA}.md")

    assert data == {"success": False, "error": REFUSAL}
    assert not (env.fx.root / f"{ALPHA}.md").exists()


def test_unchanged_catalog_paths_keep_todays_behaviour(
    env: CatalogFileDoorEnv,
) -> None:
    created = _write(env, "create_file", CATALOG_ALIAS, "xray-patterns/__any__/n.yaml")
    mismatch = _write(env, "edit_file", CATALOG_ALIAS, f"memories/{MEMORY_ALPHA}.md")
    absent = _write(env, "delete_file", CATALOG_ALIAS, "xray-patterns/__any__/x.yaml")

    assert created["success"] is True, created
    assert (env.fx.root / "xray-patterns" / "__any__" / "n.yaml").is_file()
    assert "Content hash mismatch" in mismatch["error"]
    assert absent["error"] == "File not found: xray-patterns/__any__/x.yaml"


def _projected_write_replies(env: CatalogFileDoorEnv, path: str) -> list:
    raw = (env.fx.root / path).read_bytes()
    raw_hash = hashlib.sha256(raw).hexdigest()
    requests: list = [
        (files_module.handle_edit_file, {"content_hash": "0" * 64}),
        (files_module.handle_edit_file, {"content_hash": raw_hash}),
        (files_module.handle_delete_file, {"content_hash": "0" * 64}),
    ]
    replies = []
    for handler, extra in requests:
        params: Dict[str, Any] = {
            "repository_alias": CATALOG_ALIAS,
            "file_path": path,
            "old_string": CANARY,
            "new_string": CANARY,
            **extra,
        }
        if handler is files_module.handle_delete_file:
            params.pop("old_string")
            params.pop("new_string")
        with env.serving():
            replies.append(mcp_data(handler(params, user())))
    return replies


@pytest.mark.parametrize("path", [f"{ALPHA}.md", "dependency-map/payments.md"])
def test_projected_write_replies_do_not_depend_on_hidden_text(
    env: CatalogFileDoorEnv, path: str
) -> None:
    target = env.fx.root / path
    with_hidden = _projected_write_replies(env, path)
    target.write_text(target.read_text().replace(CANARY, GAMMA))
    other_hidden = _projected_write_replies(env, path)

    assert with_hidden == other_hidden
    assert all(reply == {"success": False, "error": REFUSAL} for reply in with_hidden)


def test_admin_writes_keep_todays_behaviour(env: CatalogFileDoorEnv) -> None:
    data = _write(env, "create_file", CATALOG_ALIAS, f"{CANARY}.md", ADMIN_USER)

    assert data == {"success": False, "error": f"File already exists: {CANARY}.md"}


def test_ordinary_activation_is_not_gated(
    env: CatalogFileDoorEnv, tmp_path: Path
) -> None:
    repo = tmp_path / "activated" / "plain"
    repo.mkdir(parents=True)
    env.activations.add("plain", ALPHA, repo)

    data = _write(env, "create_file", "plain", f"{CANARY}.md")

    assert data["success"] is True, data
    assert (repo / f"{CANARY}.md").is_file()


# Every repository:write tool taking a path, with the gate that answers it.
PATH_PARAMS = {"file_path", "file_paths", "path", "paths"}
CATALOG_PATH_GATED = set(TOOLS)
# Refused for the catalog by the history policy before any path is used.
HISTORY_POLICY_GATED = {
    "git_checkout_file": "file_path",
    "git_mark_resolved": "file_path",
    "git_stage": "file_paths",
    "git_unstage": "file_paths",
}


def _write_tools_taking_a_path() -> set:
    from code_indexer.server.mcp.tools import TOOL_REGISTRY

    return {
        name
        for name, tool in TOOL_REGISTRY.items()
        if tool.get("required_permission") == "repository:write"
        and PATH_PARAMS & set((tool.get("inputSchema") or {}).get("properties", {}))
    }


def test_every_write_tool_taking_a_path_is_classified() -> None:
    assert _write_tools_taking_a_path() == CATALOG_PATH_GATED | set(
        HISTORY_POLICY_GATED
    )


@pytest.mark.parametrize("tool", sorted(HISTORY_POLICY_GATED))
@pytest.mark.parametrize("existing,missing", HIDDEN[:2])
def test_git_path_tools_refuse_the_catalog_before_any_path_is_used(
    env: CatalogFileDoorEnv, tool: str, existing: str, missing: str
) -> None:
    from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

    param = HISTORY_POLICY_GATED[tool]
    answers = []
    for path in (existing, missing):
        value: Any = [path] if param == "file_paths" else path
        args = {"repository_alias": CATALOG_ALIAS, param: value}
        with env.serving():
            answers.append(mcp_data(HANDLER_REGISTRY[tool](args, user())))

    assert answers[0] == answers[1]
    assert answers[0]["error"] == CatalogSurface.GIT_HISTORY.refusal
