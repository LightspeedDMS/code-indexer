# ruff: noqa: F811
"""Composite file doors confine the requested path to each component.

A composite listing walks the caller's ``path`` inside every component; a
path that leaves a component (parent segments, an absolute path, a symlink,
URL-encoded traversal) lists nothing from that component, exactly like a
missing path, for admins too. Each listed entry is judged on the directory
actually walked. The other composite doors (content, the activated files
route) stay confined to the composite. Real routes, real directories, real
proxy configuration on disk. Neutral names only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest

from tests.shared.cidx_meta_visibility_fixture import ADMIN_USER, ALPHA, CANARY
from tests.shared.cidx_meta_visibility_fixture import RESTRICTED_USER
from tests.unit.server.mcp.catalog_file_door_env import CatalogFileDoorEnv
from tests.unit.server.routers.test_repo_files_meta_visibility import (  # noqa: F401
    COMPOSITE,
    _get,
    client,
    composite,
    env,
)

PLAIN = "plain"
BRAVO = "bravo"
BRAVO_FILE = "bravo-only.txt"
OUTSIDE_FILE = "outside-only.txt"
OUTSIDE_MARKER = "content-outside-every-activation"
WHO = [RESTRICTED_USER, ADMIN_USER]


@pytest.fixture
def plain(env: CatalogFileDoorEnv, tmp_path: Path) -> Path:
    """An ordinary composite (``alpha`` + ``bravo``) next to a directory
    outside every activation, with symlinks in ``alpha`` leaving it."""
    import shutil

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / OUTSIDE_FILE).write_text(f"{OUTSIDE_MARKER}\n")
    root = tmp_path / "activated" / PLAIN
    shutil.copytree(env.alpha_dir, root / ALPHA)
    (root / BRAVO).mkdir()
    (root / BRAVO / BRAVO_FILE).write_text("bravo\n")
    (root / ALPHA / "to-bravo").symlink_to(root / BRAVO, target_is_directory=True)
    (root / ALPHA / "to-outside").symlink_to(outside, target_is_directory=True)
    (root / ALPHA / "bravo-file.txt").symlink_to(root / BRAVO / BRAVO_FILE)
    components = [ALPHA, BRAVO]
    (root / ".code-indexer").mkdir()
    (root / ".code-indexer" / "config.json").write_text(
        json.dumps({"proxy_mode": True, "discovered_repos": components})
    )
    env.activations.add(PLAIN, ALPHA, root)
    env.activations.records[PLAIN].update(
        is_composite=True,
        username=RESTRICTED_USER,
        path=str(root),
        golden_repo_aliases=[ALPHA],
        discovered_repos=components,
    )
    return root


def _listing(client: Dict[str, Any], alias: str, query: str, who: str) -> Any:
    response = _get(client, f"/api/repositories/{alias}/files?{query}", who)
    assert response.status_code == 200, response.text
    return response


def _entries(response: Any) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = response.json()["files"]
    return files


def _component_paths(response: Any, component: str) -> Set[str]:
    return {
        e["full_path"] for e in _entries(response) if e["component_repo"] == component
    }


def _escapes_every_component(tmp_path: Path) -> List[str]:
    outside = tmp_path / "outside"
    return [
        "../../outside",
        "%2E%2E%2F%2E%2E%2Foutside",
        str(outside),
        "../../outside/../outside",
        "to-outside",
    ]


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("recursive", ["true", "false"])
def test_path_leaving_every_component_answers_like_a_missing_path(
    client: Dict[str, Any], plain: Path, tmp_path: Path, who: str, recursive: str
) -> None:
    missing = _listing(client, PLAIN, f"recursive={recursive}&path=no-such-dir", who)

    for path in _escapes_every_component(tmp_path):
        response = _listing(client, PLAIN, f"recursive={recursive}&path={path}", who)
        assert response.json() == missing.json() == {"files": []}, path


@pytest.mark.parametrize("who", WHO)
def test_path_into_a_sibling_component_lists_nothing_from_the_first(
    client: Dict[str, Any], plain: Path, who: str
) -> None:
    for path in ("../bravo", "%2E%2E/bravo", str(plain / BRAVO), "to-bravo"):
        response = _listing(client, PLAIN, f"recursive=true&path={path}", who)
        assert _component_paths(response, ALPHA) == set(), path
        assert OUTSIDE_FILE not in response.text


@pytest.mark.parametrize("who", WHO)
def test_symlink_leaving_a_component_is_not_listed(
    client: Dict[str, Any], plain: Path, who: str
) -> None:
    for recursive in ("true", "false"):
        alpha = _component_paths(
            _listing(client, PLAIN, f"recursive={recursive}", who), ALPHA
        )
        assert not {f"{ALPHA}/to-outside", f"{ALPHA}/bravo-file.txt"} & alpha
        assert f"{ALPHA}/to-bravo" not in alpha
        assert f"{ALPHA}/src" in alpha or f"{ALPHA}/src/main.py" in alpha


@pytest.mark.parametrize("who", WHO)
def test_path_inside_a_component_still_lists_it(
    client: Dict[str, Any], plain: Path, who: str
) -> None:
    response = _listing(client, PLAIN, "recursive=true&path=src", who)

    assert _component_paths(response, ALPHA) == {f"{ALPHA}/main.py"}
    assert _component_paths(response, BRAVO) == set()


@pytest.mark.parametrize("who", WHO)
def test_catalog_composite_never_lists_the_catalog_under_another_component(
    client: Dict[str, Any], composite: Path, who: str
) -> None:
    (composite / ALPHA / "meta-link").symlink_to(
        composite / "cidx-meta", target_is_directory=True
    )
    spellings = [
        "../cidx-meta",
        "%2E%2E/cidx-meta",
        "%2E%2E%2Fcidx-meta%2Fdependency-map",
        str(composite / "cidx-meta"),
        "meta-link",
        "../../notes",
    ]
    for path in spellings:
        for recursive in ("true", "false"):
            response = _listing(
                client, COMPOSITE, f"recursive={recursive}&path={path}", who
            )
            assert _component_paths(response, ALPHA) == set(), path
            if who == RESTRICTED_USER:
                assert CANARY not in response.text, path


def test_catalog_component_is_judged_on_the_directory_walked(
    client: Dict[str, Any], composite: Path
) -> None:
    """``../cidx-meta`` walks the catalog's own root inside the catalog
    component: judged there, it shows exactly the root listing's entries."""
    root = _listing(client, COMPOSITE, "recursive=true", RESTRICTED_USER)
    dotted = _listing(
        client, COMPOSITE, "recursive=true&path=../cidx-meta", RESTRICTED_USER
    )

    assert _component_paths(dotted, "cidx-meta") == _component_paths(root, "cidx-meta")
    assert CANARY not in dotted.text


# The doors below serve composite-root-relative paths: the composite is the
# repository, so a restricted caller never gets a catalog component from it.
CATALOG_FILE = f"cidx-meta/{CANARY}.md"
CATALOG_SPELLINGS = [CATALOG_FILE, f"{ALPHA}/../{CATALOG_FILE}", f"./{CATALOG_FILE}"]


def _content(client: Dict[str, Any], alias: str, path: str, who: str) -> Any:
    return _get(
        client, f"/api/repositories/{alias}/files?content=true&path={path}", who
    )


def test_composite_content_of_a_catalog_component_answers_like_a_missing_file(
    client: Dict[str, Any], composite: Path
) -> None:
    for path in CATALOG_SPELLINGS:
        response = _content(client, COMPOSITE, path, RESTRICTED_USER)
        assert response.status_code == 404, path
        assert response.json() == {"detail": f"File '{path}' not found"}

    admin = _content(client, COMPOSITE, CATALOG_FILE, ADMIN_USER)
    assert admin.status_code == 200 and CANARY in admin.json()["content"]


def test_activated_files_route_hides_a_catalog_component(
    client: Dict[str, Any], composite: Path
) -> None:
    hidden = _get(client, f"/api/repos/{COMPOSITE}/files?path=cidx-meta&recursive=true")
    missing = _get(client, f"/api/repos/{COMPOSITE}/files?path=no-such&recursive=true")
    root = _get(client, f"/api/repos/{COMPOSITE}/files?recursive=true")
    admin = _get(client, f"/api/repos/{COMPOSITE}/files?recursive=true", ADMIN_USER)

    assert hidden.status_code == missing.status_code == 404
    assert CANARY not in hidden.text and CANARY not in root.text
    assert CANARY in admin.text


def _mcp(env: CatalogFileDoorEnv, tool: str, params: Dict[str, Any], who: str) -> str:
    from code_indexer.server.mcp.handlers import files as files_module
    from tests.unit.server.mcp.catalog_file_door_env import mcp_data, user

    with env.serving():
        result = getattr(files_module, tool)(params, user(who))
    return json.dumps(mcp_data(result))


@pytest.mark.parametrize(
    "tool", ["list_files", "browse_directory", "handle_directory_tree"]
)
def test_mcp_listings_of_a_catalog_composite_never_show_hidden_names(
    env: CatalogFileDoorEnv, composite: Path, tool: str
) -> None:
    params = {"repository_alias": COMPOSITE, "max_depth": 6}

    assert CANARY not in _mcp(env, tool, dict(params), RESTRICTED_USER)
    assert CANARY in _mcp(env, tool, dict(params), ADMIN_USER)


def test_mcp_content_of_a_catalog_component_is_not_served(
    env: CatalogFileDoorEnv, composite: Path
) -> None:
    def answer(path: str) -> str:
        params = {"repository_alias": COMPOSITE, "file_path": path}
        return _mcp(env, "get_file_content", params, RESTRICTED_USER).replace(
            path, "<path>"
        )

    missing = answer(f"{ALPHA}/no-such.md")
    assert json.loads(missing)["success"] is False
    for path in CATALOG_SPELLINGS:
        assert answer(path) == missing, path


def _root_escapes(tmp_path: Path, catalog_root: Path) -> List[str]:
    return [
        f"../notes/{CANARY}.md",
        f"%2E%2E/notes/{CANARY}.md",
        str(catalog_root / f"{CANARY}.md"),
        f"../../outside/{OUTSIDE_FILE}",
        str(tmp_path / "outside" / OUTSIDE_FILE),
        f"{ALPHA}/to-outside/{OUTSIDE_FILE}",
    ]


@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("alias", [PLAIN, COMPOSITE])
def test_composite_root_doors_never_leave_the_composite(
    client: Dict[str, Any],
    env: CatalogFileDoorEnv,
    plain: Path,
    composite: Path,
    tmp_path: Path,
    alias: str,
    who: str,
) -> None:
    for path in _root_escapes(tmp_path, env.fx.root):
        content = _content(client, alias, path, who)
        assert content.status_code == 404, (path, content.text)
        listing = _get(client, f"/api/repos/{alias}/files?path={path}", who)
        assert listing.status_code in (400, 404), (path, listing.text)
        params = {"repository_alias": alias, "file_path": path.replace("%2E", ".")}
        read = _mcp(env, "get_file_content", params, who)
        assert json.loads(read)["success"] is False, (path, read)
        for text in (content.text, listing.text, read):
            assert OUTSIDE_MARKER not in text and "Unlike alpha" not in text, path
