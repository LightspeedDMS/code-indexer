"""The catalog is recognised by the path actually served, not only by name.

Catalog identity comes from names and activation metadata, while readers
serve a resolved path. When the two disagree (an alias pointer or a clone
that is in fact the catalog), the history, temporal, X-Ray and cached
handle decisions treat the request as the catalog for a restricted caller
(fail closed). Admins are not affected. Real git repositories, a real alias
registry and a real ActivatedRepoManager (the fixture of
test_meta_history_surfaces_denied). Neutral names only.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.cache.handle_binding import (
    mint_handle,
    reset_handle_binding,
)
from code_indexer.server.services.cidx_meta_access.cached_handles import (
    bind_catalog_handles,
)
from code_indexer.server.services.cidx_meta_access.history_policy import (
    CatalogSurface,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    RESTRICTED_USER,
)

from . import test_meta_history_surfaces_denied as _history
from .test_meta_history_surfaces_denied import _activate, _args_for, _call, _user

# The shared real-repository fixtures, re-exported for pytest.
module_tmp = _history.module_tmp
fx = _history.fx
server = _history.server

GIT_REFUSAL = CatalogSurface.GIT_HISTORY.refusal
POINTED = f"{ALPHA}-global"
STALE = "stale-source"


@pytest.fixture
def pointer_to_catalog(server: SimpleNamespace) -> Iterator[str]:
    pointer = Path(server.state.golden_repos_dir) / "aliases" / f"{POINTED}.json"
    original = pointer.read_bytes()
    data = json.loads(original)
    data["target_path"] = str(server.catalog)
    pointer.write_text(json.dumps(data))
    try:
        yield POINTED
    finally:
        pointer.write_bytes(original)


@pytest.fixture(scope="module")
def stale_activation(server: SimpleNamespace) -> str:
    arm = server.state.activated_repo_manager
    _activate(
        Path(arm.activated_repos_dir).parent, server.catalog, RESTRICTED_USER, STALE
    )
    meta = Path(arm.activated_repos_dir) / RESTRICTED_USER / f"{STALE}_metadata.json"
    record = json.loads(meta.read_text())
    record["golden_repo_alias"] = ALPHA
    meta.write_text(json.dumps(record))
    return STALE


def _restricted(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return _call(tool, args, _user(RESTRICTED_USER, UserRole.NORMAL_USER))


def test_git_history_through_a_pointer_to_the_catalog_is_refused(
    pointer_to_catalog: str,
) -> None:
    data = _restricted("git_log", _args_for("git_log", pointer_to_catalog))
    assert data.get("error") == GIT_REFUSAL, data


def test_admin_reads_history_through_the_same_pointer(pointer_to_catalog: str) -> None:
    args = _args_for("git_log", pointer_to_catalog)
    data = _call("git_log", args, _user(ADMIN_USER, UserRole.ADMIN))
    assert data.get("success") is True, data


def test_untouched_granted_alias_keeps_its_history(server: SimpleNamespace) -> None:
    data = _restricted("git_log", _args_for("git_log", POINTED))
    assert data.get("success") is True, data


def test_xray_through_a_pointer_to_the_catalog_is_refused(
    pointer_to_catalog: str,
) -> None:
    from .test_meta_history_surfaces_denied import _xray_args

    data = _restricted("xray_search", _xray_args("xray_search", pointer_to_catalog))
    assert CatalogSurface.XRAY.refusal in json.dumps(data), data


def test_handles_minted_through_a_pointer_to_the_catalog_are_bound(
    server: SimpleNamespace, fx: Any, pointer_to_catalog: str
) -> None:
    token = bind_catalog_handles(
        pointer_to_catalog,
        RESTRICTED_USER,
        fx.access_service,
        server.state.activated_repo_manager,
    )
    try:
        assert ".cmv1." in mint_handle("sample")
    finally:
        reset_handle_binding(token)


def test_git_history_of_a_clone_of_the_catalog_is_refused(
    stale_activation: str,
) -> None:
    data = _restricted("git_log", _args_for("git_log", stale_activation))
    assert data.get("error") == GIT_REFUSAL, data


def test_temporal_search_of_a_clone_of_the_catalog_is_refused(
    stale_activation: str,
) -> None:
    args = {
        "repository_alias": stale_activation,
        "query_text": "x",
        "time_range_all": True,
    }
    data = _restricted("search_code", args)
    assert data.get("error") == CatalogSurface.TEMPORAL.refusal, data


@pytest.fixture
def golden_layout(tmp_path: Path) -> SimpleNamespace:
    golden = tmp_path / "data" / "golden-repos"
    base = golden / "cidx-meta"
    base.mkdir(parents=True)
    other = golden / ALPHA
    other.mkdir()
    (golden / "aliases").mkdir()
    AliasManager(str(golden / "aliases")).create_alias("cidx-meta-global", str(base))
    return SimpleNamespace(golden=golden, base=base, other=other)


def test_catalog_locations_are_recognised(golden_layout: SimpleNamespace) -> None:
    from code_indexer.server.services.cidx_meta_access.served_identity import (
        path_is_catalog_location,
    )

    golden = str(golden_layout.golden)
    snapshot = golden_layout.golden / ".versioned" / "cidx-meta" / "v_1700000000"
    ordinary_snapshot = golden_layout.golden / ".versioned" / ALPHA / "v_1700000000"
    assert path_is_catalog_location(str(golden_layout.base), golden)
    assert path_is_catalog_location(str(snapshot), golden)
    assert not path_is_catalog_location(str(ordinary_snapshot), golden)
    assert not path_is_catalog_location(str(golden_layout.other), golden)
