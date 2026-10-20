"""REST file doors serve the catalog as the caller may see it.

The activation used here names a repository the caller is granted while its
directory IS the catalog: the doors judge the directory they serve, so the
catalog restriction applies whatever the activation record says. A hidden
path answers exactly like a missing one, dependency-map documents and
descriptions are projected, and admins get the files unchanged. Real routes
on a small FastAPI app, real catalog on disk, real FileListingService and
group store. Neutral names only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import (
    get_current_user,
    get_current_user_hybrid,
)
from code_indexer.server.routers import repository_health
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.cidx_meta_access import RESTRICTED_PLACEHOLDER
from code_indexer.server.services.file_service import file_service
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    RESTRICTED_USER,
    payments_lines,
)
from tests.unit.server.mcp.catalog_file_door_env import CatalogFileDoorEnv, user

NOTES = "notes"
MISSING = "xray-patterns/__any__/missing.yaml"


@pytest.fixture
def env(tmp_path: Path) -> CatalogFileDoorEnv:
    env = CatalogFileDoorEnv(tmp_path)
    linked = tmp_path / "activated" / NOTES
    linked.parent.mkdir(parents=True)
    linked.symlink_to(env.fx.root)
    env.activations.add(NOTES, ALPHA, linked)
    return env


@pytest.fixture
def client(env: CatalogFileDoorEnv) -> Iterator[Dict[str, Any]]:
    from code_indexer.server.routers.inline_repos import register_repo_routes
    from code_indexer.server.routers.inline_repos_v2 import register_repos_v2_routes
    from code_indexer.server.routers.repository_health import router

    app = FastAPI()
    for name, value in vars(env.state).items():
        setattr(app.state, name, value)
    app.state.access_filtering_service = AccessFilteringService(
        env.fx.group_manager,
        memories_dir=env.fx.root / "memories",
        activated_repo_manager=env.activations,  # type: ignore[arg-type]
    )
    common = dict(
        activated_repo_manager=env.activations,
        repository_listing_manager=None,
        background_job_manager=None,
    )
    register_repo_routes(app, golden_repo_manager=None, **common)
    register_repos_v2_routes(app, **common)
    app.include_router(router)
    current = {"user": user()}
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.dependency_overrides[get_current_user_hybrid] = lambda: current["user"]
    previous = file_service._activated_repo_manager_lazy
    file_service.activated_repo_manager = env.activations  # type: ignore[assignment]
    service = app.state.access_filtering_service
    try:
        # The description route reads its access service off the app module.
        with (
            patch.object(
                repository_health, "_get_access_filtering_service", lambda: service
            ),
            TestClient(app, raise_server_exceptions=False) as test_client,
        ):
            yield {"client": test_client, "current": current}
    finally:
        file_service._activated_repo_manager_lazy = previous


def _get(client: Dict[str, Any], url: str, who: str = RESTRICTED_USER) -> Any:
    client["current"]["user"] = user(who)
    return client["client"].get(url)


def _no_hidden(text: str) -> None:
    assert CANARY not in text and f"{GAMMA}." not in text


def _v2_content(client: Dict[str, Any], path: str, who: str = RESTRICTED_USER) -> Any:
    return _get(
        client, f"/api/repositories/{NOTES}/files?content=true&path={path}", who
    )


def test_v2_listing_of_a_directory_serving_the_catalog_is_restricted(
    client: Dict[str, Any],
) -> None:
    response = _get(client, f"/api/repositories/{NOTES}/files?limit=500")

    assert response.status_code == 200, response.text
    paths = {f["path"] for f in response.json()["files"]}
    assert "dependency-map/payments.md" in paths and f"{ALPHA}.md" in paths
    assert response.json()["pagination"]["total"] == len(paths)
    _no_hidden(response.text)


@pytest.mark.parametrize(
    "hidden",
    [f"{CANARY}.md", "dependency-map/_domains.json", ".code-indexer/index/meta.json"],
)
def test_v2_hidden_content_answers_like_a_missing_file(
    client: Dict[str, Any], hidden: str
) -> None:
    denied = _v2_content(client, hidden)
    missing = _v2_content(client, MISSING)

    assert missing.status_code == 404
    assert denied.status_code == 404
    assert denied.text.replace(hidden, "<p>") == missing.text.replace(MISSING, "<p>")


def test_v2_content_is_projected(client: Dict[str, Any]) -> None:
    payments = _v2_content(client, "dependency-map/payments.md")
    alpha = _v2_content(client, f"{ALPHA}.md")

    assert payments.status_code == 200, payments.text
    assert len(payments.json()["content"].split("\n")) - 1 == len(payments_lines())
    assert RESTRICTED_PLACEHOLDER in alpha.json()["content"]
    _no_hidden(payments.text)
    _no_hidden(alpha.text)


def test_v2_admin_content_is_unchanged(
    client: Dict[str, Any], env: CatalogFileDoorEnv
) -> None:
    response = _v2_content(client, f"{CANARY}.md", ADMIN_USER)

    assert response.status_code == 200
    assert response.json()["content"] == (env.fx.root / f"{CANARY}.md").read_text()


@pytest.mark.parametrize("recursive", ["false", "true"])
def test_activated_files_route_lists_only_visible_entries(
    client: Dict[str, Any], recursive: str
) -> None:
    response = _get(client, f"/api/repos/{NOTES}/files?recursive={recursive}")

    assert response.status_code == 200, response.text
    names = {f["path"] for f in response.json()["files"]}
    assert f"{ALPHA}.md" in names and "memories" in names
    assert not {".code-indexer", ".gitignore", ".git"} & names
    _no_hidden(response.text)


def test_activated_files_route_hidden_base_answers_like_a_missing_path(
    client: Dict[str, Any],
) -> None:
    hidden = _get(client, f"/api/repos/{NOTES}/files?path=xray-patterns/{CANARY}")
    missing = _get(client, f"/api/repos/{NOTES}/files?path=no-such-dir")

    assert missing.status_code == 404
    assert hidden.status_code == 404
    assert hidden.text.replace(
        f"xray-patterns/{CANARY}", "<p>"
    ) == missing.text.replace("no-such-dir", "<p>")


def test_admin_activated_listing_is_unchanged(client: Dict[str, Any]) -> None:
    response = _get(client, f"/api/repos/{NOTES}/files", ADMIN_USER)

    assert f"{CANARY}.md" in {f["path"] for f in response.json()["files"]}


COMPOSITE = "combined"
CATALOG_VISIBLE = {
    f"{ALPHA}.md",
    "dependency-map/_index.md",
    "dependency-map/payments.md",
    "dependency-map/ledger.md",
    "dependency-map/_dep_types.md",
    "dependency-map/_analysis_guidelines.md",
    "xray-patterns/__any__/p.yaml",
}


@pytest.fixture
def composite(env: CatalogFileDoorEnv, tmp_path: Path) -> Path:
    """A pre-existing composite activation whose record names only a granted
    repository while one component directory is a catalog copy (real proxy
    configuration on disk): the directory served decides."""
    import shutil

    root = tmp_path / "activated" / COMPOSITE
    shutil.copytree(env.fx.root, root / "cidx-meta", symlinks=True)
    shutil.copytree(env.alpha_dir, root / ALPHA)
    components = ["cidx-meta", ALPHA]
    (root / ".code-indexer").mkdir()
    (root / ".code-indexer" / "config.json").write_text(
        json.dumps({"proxy_mode": True, "discovered_repos": components})
    )
    env.activations.add(COMPOSITE, ALPHA, root)
    env.activations.records[COMPOSITE].update(
        is_composite=True,
        username=RESTRICTED_USER,
        path=str(root),
        golden_repo_aliases=[ALPHA],
        discovered_repos=components,
    )
    return root


def _composite_paths(
    client: Dict[str, Any], query: str, who: str = RESTRICTED_USER
) -> Any:
    response = _get(client, f"/api/repositories/{COMPOSITE}/files?{query}", who)
    assert response.status_code == 200, response.text
    return {f["full_path"] for f in response.json()["files"]}, response.text


def test_composite_listing_judges_each_component_on_its_own_paths(
    client: Dict[str, Any], composite: Path
) -> None:
    paths, text = _composite_paths(client, "recursive=true")

    memories = {p for p in paths if p.startswith("cidx-meta/memories/")}
    catalog = {p for p in paths if p.startswith("cidx-meta/")} - memories
    assert catalog == {f"cidx-meta/{p}" for p in CATALOG_VISIBLE}
    assert len(memories) == 2
    assert f"{ALPHA}/src/main.py" in paths
    _no_hidden(text)


def test_composite_subdirectory_listing_uses_catalog_relative_paths(
    client: Dict[str, Any], composite: Path
) -> None:
    paths, text = _composite_paths(client, "recursive=true&path=dependency-map")

    shown = {p.split("/", 1)[1] for p in paths}
    expected = {
        p.split("/", 1)[1] for p in CATALOG_VISIBLE if p.startswith("dependency-map/")
    }
    assert shown == expected
    _no_hidden(text)


def test_composite_single_level_listing_hides_internals_and_hidden_names(
    client: Dict[str, Any], composite: Path
) -> None:
    paths, text = _composite_paths(client, "recursive=false")

    assert paths == {
        f"cidx-meta/{ALPHA}.md",
        "cidx-meta/dependency-map",
        "cidx-meta/memories",
        "cidx-meta/xray-patterns",
        f"{ALPHA}/src",
    }
    _no_hidden(text)


def test_admin_composite_listing_is_unchanged(
    client: Dict[str, Any], composite: Path
) -> None:
    paths, _ = _composite_paths(client, "recursive=true", ADMIN_USER)

    assert f"cidx-meta/{CANARY}.md" in paths


def test_description_is_redacted_for_a_restricted_caller(
    client: Dict[str, Any], env: CatalogFileDoorEnv
) -> None:
    (env.golden_root / "cidx-meta").symlink_to(env.fx.root)

    restricted = _get(client, f"/api/repositories/{ALPHA}/description")
    admin = _get(client, f"/api/repositories/{ALPHA}/description", ADMIN_USER)

    assert restricted.status_code == 200, restricted.text
    assert RESTRICTED_PLACEHOLDER in restricted.json()["description"]
    _no_hidden(json.dumps(restricted.json()))
    assert CANARY in admin.json()["description"]
