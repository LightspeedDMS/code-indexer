"""REST file doors serve the catalog as the caller may see it, on a
mount-less pod.

``test_repo_files_meta_visibility`` runs these doors on this disk. Here the
server's content backend is NOT local: the activated copy is read and listed
through the content seam (a non-local backend over the very same tree), with
every local read of the file service made to fail. The doors must still
judge the directory they serve BEFORE reading: hidden paths answer like
missing ones, descriptions and dependency-map documents are projected, and
admins get the files unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, cast
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import (
    get_current_user,
    get_current_user_hybrid,
)
from code_indexer.server.routers import repository_health
from code_indexer.server.services import file_crud_service
from code_indexer.server.services import file_service as fs_mod
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.cidx_meta_access import RESTRICTED_PLACEHOLDER
from code_indexer.server.services.cidx_meta_read_mirror import reset_mirrors
from code_indexer.server.services.file_service import file_service
from code_indexer.server.startup import content_backend_wiring as wiring
from code_indexer.server.startup import content_writer_wiring as writer_wiring
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    RESTRICTED_USER,
    payments_lines,
)
from tests.unit.server.mcp.catalog_delegated_env import (
    DelegatedCatalogBackend,
    MappedCatalogBackend,
)
from tests.unit.server.mcp.catalog_file_door_env import CatalogFileDoorEnv, user

NOTES = "notes"
MISSING = "xray-patterns/__any__/missing.yaml"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CatalogFileDoorEnv:
    env = CatalogFileDoorEnv(tmp_path)
    linked = tmp_path / "activated" / NOTES
    linked.parent.mkdir(parents=True)
    linked.symlink_to(env.fx.root)
    env.activations.add(NOTES, ALPHA, linked)
    backend = DelegatedCatalogBackend()
    monkeypatch.setattr(wiring, "server_content_backend", lambda: backend)
    monkeypatch.setattr(wiring, "_golden_repo_record", lambda name: None)
    monkeypatch.setattr(writer_wiring, "server_content_writer", lambda: object())
    # Under delegation "is it activated" is the registry's answer.
    monkeypatch.setattr(
        file_crud_service, "_get_activated_repo_manager", lambda: env.activations
    )
    reset_mirrors()
    fs_mod._collect_cache.clear()
    fs_mod.clear_manifest_cache()
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
        activated_repo_manager=cast(Any, env.activations),
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
    file_service.activated_repo_manager = cast(Any, env.activations)
    service = app.state.access_filtering_service
    try:
        with (
            patch.object(
                repository_health, "_get_access_filtering_service", lambda: service
            ),
            TestClient(app, raise_server_exceptions=False) as test_client,
        ):
            yield {"client": test_client, "current": current}
    finally:
        file_service._activated_repo_manager_lazy = previous
        reset_mirrors()
        fs_mod._collect_cache.clear()
        fs_mod.clear_manifest_cache()


def _get(client: Dict[str, Any], url: str, who: str = RESTRICTED_USER) -> Any:
    client["current"]["user"] = user(who)
    return client["client"].get(url)


def _no_hidden(text: str) -> None:
    assert CANARY not in text and f"{GAMMA}." not in text


def _v2_content(client: Dict[str, Any], path: str, who: str = RESTRICTED_USER) -> Any:
    return _get(
        client, f"/api/repositories/{NOTES}/files?content=true&path={path}", who
    )


@pytest.mark.parametrize("hidden", [f"{CANARY}.md", "dependency-map/_domains.json"])
def test_v2_hidden_content_answers_like_a_missing_file_through_the_seam(
    client: Dict[str, Any], hidden: str
) -> None:
    denied = _v2_content(client, hidden)
    missing = _v2_content(client, MISSING)

    assert missing.status_code == 404
    assert denied.status_code == 404
    assert denied.text.replace(hidden, "<p>") == missing.text.replace(MISSING, "<p>")


def test_v2_content_is_projected_through_the_seam(client: Dict[str, Any]) -> None:
    payments = _v2_content(client, "dependency-map/payments.md")
    alpha = _v2_content(client, f"{ALPHA}.md")

    assert payments.status_code == 200, payments.text
    body = payments.json()
    assert len(body["content"].split("\n")) - 1 == len(payments_lines())
    assert body["size"] == len(body["content"].encode("utf-8"))
    assert RESTRICTED_PLACEHOLDER in alpha.json()["content"]
    _no_hidden(payments.text)
    _no_hidden(alpha.text)


def test_v2_admin_content_is_unchanged_through_the_seam(
    client: Dict[str, Any], env: CatalogFileDoorEnv
) -> None:
    response = _v2_content(client, f"{CANARY}.md", ADMIN_USER)

    assert response.status_code == 200
    assert response.json()["content"] == (env.fx.root / f"{CANARY}.md").read_text()


def test_v2_listing_is_restricted_through_the_seam(client: Dict[str, Any]) -> None:
    response = _get(client, f"/api/repositories/{NOTES}/files?limit=500")

    assert response.status_code == 200, response.text
    paths = {f["path"] for f in response.json()["files"]}
    assert "dependency-map/payments.md" in paths and f"{ALPHA}.md" in paths
    _no_hidden(response.text)


@pytest.mark.parametrize("recursive", ["false", "true"])
def test_activated_files_route_lists_only_visible_entries_through_the_seam(
    client: Dict[str, Any], recursive: str
) -> None:
    response = _get(client, f"/api/repos/{NOTES}/files?recursive={recursive}")

    assert response.status_code == 200, response.text
    names = {f["path"] for f in response.json()["files"]}
    assert f"{ALPHA}.md" in names and "memories" in names
    _no_hidden(response.text)


def test_activated_files_route_hidden_base_answers_like_a_missing_path_via_seam(
    client: Dict[str, Any],
) -> None:
    hidden = _get(client, f"/api/repos/{NOTES}/files?path=xray-patterns/{CANARY}")
    missing = _get(client, f"/api/repos/{NOTES}/files?path=no-such-dir")

    assert missing.status_code == 404
    assert hidden.status_code == 404
    assert hidden.text.replace(
        f"xray-patterns/{CANARY}", "<p>"
    ) == missing.text.replace("no-such-dir", "<p>")


def test_admin_activated_listing_is_unchanged_through_the_seam(
    client: Dict[str, Any],
) -> None:
    response = _get(client, f"/api/repos/{NOTES}/files", ADMIN_USER)

    assert f"{CANARY}.md" in {f["path"] for f in response.json()["files"]}


@pytest.fixture
def pod_catalog(env: CatalogFileDoorEnv, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The pod's own ``golden-repos/cidx-meta``: index state only, its
    content held by the backend (workspace-mcp) at the same path."""
    pod = env.golden_root / "cidx-meta"
    (pod / ".code-indexer").mkdir(parents=True)
    backend = MappedCatalogBackend(pod, env.fx.root)
    monkeypatch.setattr(wiring, "server_content_backend", lambda: backend)
    return pod


def test_description_is_projected_through_the_seam(
    client: Dict[str, Any], pod_catalog: Path
) -> None:
    restricted = _get(client, f"/api/repositories/{ALPHA}/description")
    admin = _get(client, f"/api/repositories/{ALPHA}/description", ADMIN_USER)

    assert restricted.status_code == 200, restricted.text
    assert RESTRICTED_PLACEHOLDER in restricted.json()["description"]
    _no_hidden(json.dumps(restricted.json()))
    assert admin.status_code == 200, admin.text
    assert CANARY in admin.json()["description"]


def test_hidden_description_answers_not_found_through_the_seam(
    client: Dict[str, Any], pod_catalog: Path
) -> None:
    from code_indexer.server.routers.repository_health import _read_description

    with pytest.raises(FileNotFoundError):
        _read_description(
            client["client"].app.state, pod_catalog.parent, CANARY, RESTRICTED_USER
        )


def test_seam_description_reads_only_a_file_directly_in_the_catalog(
    client: Dict[str, Any], pod_catalog: Path, env: CatalogFileDoorEnv
) -> None:
    """An alias naming a nested catalog file is not a description, even for
    an admin and even though the file exists on the backend."""
    from code_indexer.server.routers.repository_health import _read_description

    nested = next((env.fx.root / "memories").glob("*.md"))
    alias = f"memories/{nested.stem}"

    with pytest.raises(FileNotFoundError):
        _read_description(
            client["client"].app.state, pod_catalog.parent, alias, ADMIN_USER
        )
