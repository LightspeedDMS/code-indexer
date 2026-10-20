"""directory_tree judges the canonical tree root of a catalog request.

For a restricted caller the requested path is resolved (dot segments and
in-repository symlinks) before anything is walked, and the decision is made
on that canonical path: a target the caller may not see is answered exactly
like a path that does not exist; a visible document is a valid tree root.
Without an access service the browse and tree doors show nothing. Real
catalog on disk, real registry and group store. Neutral names only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.server.mcp.handlers import files as files_module
from tests.shared.cidx_meta_visibility_fixture import ADMIN_USER, CANARY, GAMMA

from .catalog_file_door_env import CATALOG_ALIAS, CatalogFileDoorEnv, mcp_data, user

MISSING_DIR = "xray-patterns/__any__/missing-dir"


@pytest.fixture
def env(tmp_path: Path) -> CatalogFileDoorEnv:
    return CatalogFileDoorEnv(tmp_path)


def _tree(env: CatalogFileDoorEnv, path: str, who: str = "") -> Dict[str, Any]:
    params = {"repository_alias": CATALOG_ALIAS, "path": path, "max_depth": 5}
    with env.serving():
        return mcp_data(
            files_module.handle_directory_tree(params, user(who) if who else user())
        )


def _without(data: Dict[str, Any], echo: str) -> str:
    return json.dumps(data).replace(echo, "<path>")


def _link(env: CatalogFileDoorEnv, link: str, target: str) -> None:
    (env.fx.root / link).symlink_to(env.fx.root / target, target_is_directory=True)


def test_link_to_a_hidden_directory_answers_like_a_missing_path(
    env: CatalogFileDoorEnv,
) -> None:
    _link(env, "xray-patterns/__any__/hidden-link", f"xray-patterns/{CANARY}")

    denied = _tree(env, "xray-patterns/__any__/hidden-link")
    missing = _tree(env, MISSING_DIR)

    assert missing["success"] is False
    assert _without(denied, "xray-patterns/__any__/hidden-link") == _without(
        missing, MISSING_DIR
    )


def test_dot_segments_to_a_hidden_directory_answer_like_a_missing_path(
    env: CatalogFileDoorEnv,
) -> None:
    spelled = f"xray-patterns/__any__/../{CANARY}"

    denied = _tree(env, spelled)
    missing = _tree(env, MISSING_DIR)

    assert _without(denied, spelled) == _without(missing, MISSING_DIR)


def test_link_to_a_visible_directory_is_walked_from_its_target(
    env: CatalogFileDoorEnv,
) -> None:
    _link(env, "xray-patterns/shared-link", "xray-patterns/__any__")

    data = _tree(env, "xray-patterns/shared-link")

    assert data["success"] is True, data
    assert data["total_files"] == 1 and "p.yaml" in data["tree_string"]
    assert CANARY not in json.dumps(data)


def test_visible_dependency_map_document_is_a_tree_root(
    env: CatalogFileDoorEnv,
) -> None:
    data = _tree(env, "dependency-map/payments.md")

    assert data["success"] is True, data
    assert data["total_files"] == 1
    assert data["root"]["name"] == "payments.md"
    assert CANARY not in json.dumps(data) and f"{GAMMA}." not in json.dumps(data)


def test_hidden_dependency_map_document_answers_like_a_missing_document(
    env: CatalogFileDoorEnv,
) -> None:
    hidden = f"dependency-map/{CANARY}.md"

    denied = _tree(env, hidden)
    missing = _tree(env, "dependency-map/missing.md")

    assert missing["success"] is False
    assert _without(denied, hidden) == _without(missing, "dependency-map/missing.md")


def test_admin_tree_of_a_link_is_unchanged(env: CatalogFileDoorEnv) -> None:
    _link(env, "xray-patterns/__any__/hidden-link", f"xray-patterns/{CANARY}")

    data = _tree(env, "xray-patterns/__any__/hidden-link", ADMIN_USER)

    assert data["success"] is True, data
    assert data["total_files"] == 1


def test_browse_and_tree_show_nothing_without_an_access_service(
    env: CatalogFileDoorEnv,
) -> None:
    env.state.access_filtering_service = None
    params = {"repository_alias": CATALOG_ALIAS}
    with env.serving():
        browsed = mcp_data(files_module.browse_directory(dict(params), user()))
        tree = mcp_data(
            files_module.handle_directory_tree({**params, "max_depth": 5}, user())
        )

    assert browsed["success"] is True and browsed["structure"]["files"] == []
    assert tree["success"] is True and tree["total_files"] == 0, tree
    for data in (browsed, tree):
        serialized = json.dumps(data)
        assert CANARY not in serialized and "alpha.md" not in serialized
