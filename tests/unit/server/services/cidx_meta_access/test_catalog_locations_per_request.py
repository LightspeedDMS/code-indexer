"""The catalog's locations are computed once per request; alias shapes the
gates cannot judge fail closed in one shared helper.

``catalog_locations`` (an alias-pointer read plus ``realpath`` calls) is
computed at most once per request however many aliases or served
directories the request names. Cached-handle binding judges alias shapes
with the history policy's helper, so a tuple, an empty collection, a
non-string, a wildcard or an over-nested list binds the restricted caller's
visibility. Real catalog fixture, real alias pointers, real group store; the
location count is observed through a pass-through spy. Neutral names only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, List, Tuple

import pytest

from code_indexer.server.cache.handle_binding import (
    handle_binding,
    mint_handle,
    reset_handle_binding,
)
from code_indexer.server.services.cidx_meta_access import served_identity
from code_indexer.server.services.cidx_meta_access.cached_handles import (
    bind_catalog_handles,
)
from code_indexer.server.services.cidx_meta_access.history_policy import (
    CatalogSurface,
    catalog_surface_refusal,
    golden_catalog_surface_refusal,
)
from tests.shared.cidx_meta_visibility_fixture import ALPHA, GAMMA, RESTRICTED_USER
from tests.unit.server.mcp.catalog_file_door_env import CatalogFileDoorEnv, point_alias

NAMES = [ALPHA, GAMMA, "zed"]


@pytest.fixture
def env(tmp_path: Path) -> CatalogFileDoorEnv:
    env = CatalogFileDoorEnv(tmp_path)
    for name in NAMES:
        point_alias(env.golden_root, f"{name}-global", env.alpha_dir)
    env.activations.data_dir = str(env.golden_root.parent)  # type: ignore[attr-defined]
    return env


@pytest.fixture
def location_calls(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    calls: List[str] = []
    real: Callable[[str], Tuple[str, ...]] = served_identity.catalog_locations

    def counted(golden_repos_dir: str) -> Tuple[str, ...]:
        calls.append(golden_repos_dir)
        return real(golden_repos_dir)

    monkeypatch.setattr(served_identity, "catalog_locations", counted)
    return calls


def test_activation_gate_computes_locations_once(
    env: CatalogFileDoorEnv, location_calls: List[str]
) -> None:
    refusal = catalog_surface_refusal(
        CatalogSurface.GIT_HISTORY,
        NAMES,
        RESTRICTED_USER,
        env.fx.access_service,
        env.activations,
    )

    assert refusal is None
    assert len(location_calls) == 1


def test_golden_gate_computes_locations_once(
    env: CatalogFileDoorEnv, location_calls: List[str]
) -> None:
    refusal = golden_catalog_surface_refusal(
        CatalogSurface.TEMPORAL,
        NAMES,
        RESTRICTED_USER,
        env.fx.access_service,
        str(env.golden_root),
    )

    assert refusal is None
    assert len(location_calls) == 1


def _served_history(env: CatalogFileDoorEnv, state: Any, who: str) -> None:
    from code_indexer.server.routers.repo_access_http import (
        refuse_served_catalog_history,
    )

    refuse_served_catalog_history(
        state, env.activations, who, f"{ALPHA}-global", env.alpha_dir
    )


def test_served_history_computes_locations_once(
    env: CatalogFileDoorEnv, location_calls: List[str]
) -> None:
    _served_history(env, env.state, RESTRICTED_USER)

    assert len(location_calls) == 1


def test_served_history_without_golden_dir_refuses_restricted(
    env: CatalogFileDoorEnv,
) -> None:
    from types import SimpleNamespace

    from fastapi import HTTPException

    from tests.shared.cidx_meta_visibility_fixture import ADMIN_USER

    state = SimpleNamespace(**{**vars(env.state), "golden_repos_dir": None})

    with pytest.raises(HTTPException) as refused:
        _served_history(env, state, RESTRICTED_USER)
    assert refused.value.status_code == 403
    _served_history(env, state, ADMIN_USER)


def test_served_history_of_an_ordinary_repository_passes(
    env: CatalogFileDoorEnv,
) -> None:
    _served_history(env, env.state, RESTRICTED_USER)


def _bound(env: CatalogFileDoorEnv, aliases: Any) -> Any:
    token = bind_catalog_handles(
        aliases, RESTRICTED_USER, env.fx.access_service, env.activations
    )
    try:
        return handle_binding(mint_handle("h"))
    finally:
        reset_handle_binding(token)


def test_cached_handles_compute_locations_once(
    env: CatalogFileDoorEnv, location_calls: List[str]
) -> None:
    assert _bound(env, [f"{name}-global" for name in NAMES]) is None
    assert len(location_calls) == 1


@pytest.mark.parametrize(
    "aliases",
    [("cidx-meta-global",), [], (), "", 7, None, "alp*", [[[[[f"{ALPHA}-global"]]]]]],
)
def test_cached_handles_bind_unjudgeable_shapes(
    env: CatalogFileDoorEnv, aliases: Any
) -> None:
    assert _bound(env, aliases) is not None


def test_cached_handles_leave_an_ordinary_alias_unbound(
    env: CatalogFileDoorEnv,
) -> None:
    assert _bound(env, f"{ALPHA}-global") is None
    assert _bound(env, (f"{ALPHA}-global",)) is None
    assert _bound(env, "cidx-meta-global") is not None
