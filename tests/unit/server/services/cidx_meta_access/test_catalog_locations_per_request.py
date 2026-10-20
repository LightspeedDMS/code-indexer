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

import json
import os
import sys
import threading
from collections import Counter
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
from code_indexer.server.services.cidx_meta_access.file_doors import (
    judge_app_request,
    view_for,
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


def test_catalog_locations_never_create_the_aliases_directory(
    tmp_path: Path,
) -> None:
    golden = tmp_path / "data" / "golden-repos"
    golden.mkdir(parents=True)

    _ = served_identity.catalog_locations(str(golden))
    _ = served_identity.request_served_paths(f"{ALPHA}-global", "u", None, str(golden))

    assert not (golden / "aliases").exists()


_OPENED: List[str] = []
_RECORDING_THREADS: List[int] = []


def _record_opens(event: str, args: Tuple[Any, ...]) -> None:
    # ^ why audit hooks cannot be removed=>no-op outside a recording thread
    if event != "open" or threading.get_ident() not in _RECORDING_THREADS:
        return
    target = args[0]
    if isinstance(target, (str, bytes, os.PathLike)):
        _OPENED.append(os.fsdecode(os.fspath(target)))


sys.addaudithook(_record_opens)


def _ordinary_clone(env: CatalogFileDoorEnv) -> Path:
    git = env.alpha_dir / ".git"
    git.mkdir()
    (git / "config").write_text(
        '[remote "origin"]\n\turl = https://example.com/alpha.git\n'
    )
    config = env.alpha_dir / ".code-indexer" / "config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"embedding_provider": "voyage-ai"}))
    return env.alpha_dir


def _judge_and_serve(env: CatalogFileDoorEnv, served: Path) -> None:
    access = judge_app_request(
        env.state, f"{ALPHA}-global", str(served), RESTRICTED_USER, env.activations
    )
    assert access.view is None
    assert view_for(str(served), access, FileNotFoundError("x")) is None


def test_restricted_judgement_opens_each_identity_file_once(
    env: CatalogFileDoorEnv,
) -> None:
    served = _ordinary_clone(env)
    _OPENED.clear()
    _RECORDING_THREADS.append(threading.get_ident())
    try:
        _judge_and_serve(env, served)
    finally:
        _RECORDING_THREADS.clear()

    counts = Counter(_OPENED)
    assert counts[str(served / ".git" / "config")] == 1, counts
    assert counts[str(served / ".code-indexer" / "config.json")] == 1, counts
    aliases = [p for p in counts if "/aliases/" in p]
    assert aliases, counts
    assert all(counts[p] == 1 for p in aliases), counts


def test_restricted_judgement_resolves_the_served_path_once(
    env: CatalogFileDoorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = _ordinary_clone(env)
    calls: List[str] = []
    real = os.path.realpath

    def counted(path: Any, *args: Any, **kwargs: Any) -> str:
        calls.append(os.fsdecode(os.fspath(path)))
        return os.fsdecode(real(path, *args, **kwargs))

    monkeypatch.setattr(os.path, "realpath", counted)
    _judge_and_serve(env, served)
    monkeypatch.undo()

    assert calls.count(str(served)) == 1, calls


def _set_pointer(env: CatalogFileDoorEnv, opaque: Path, state: str) -> None:
    pointer = env.golden_root / "aliases" / "cidx-meta-global.json"
    if pointer.is_symlink() or pointer.is_file():
        pointer.unlink()
    if state == "valid":
        pointer.write_text(json.dumps({"target_path": str(opaque)}))
    elif state == "non-object":
        pointer.write_text("[]")
    elif state == "dangling":
        pointer.symlink_to(pointer.parent / "missing.json")
    elif state == "dirlink":
        pointer.symlink_to(opaque, target_is_directory=True)


def test_search_for_caller_restricts_an_opaque_directory_while_the_pointer_is_unusable(
    env: CatalogFileDoorEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from code_indexer.server import app as app_module
    from code_indexer.server.services.cidx_meta_access.catalog_search import (
        TextRematch,
        search_for_caller,
    )

    opaque = tmp_path / "opaque-served"
    opaque.mkdir()
    monkeypatch.setattr(
        app_module, "app", SimpleNamespace(state=env.state), raising=False
    )

    def rounds(state: str) -> List[int]:
        _set_pointer(env, opaque, state)
        sizes: List[int] = []

        def run(k: int, *rest: Any, **kwargs: Any) -> List[Any]:
            sizes.append(k)
            return []

        search_for_caller(
            run,
            username=RESTRICTED_USER,
            repo_info={"is_global": True, "user_alias": "ordinary-global"},
            repo_path=str(opaque),
            activated_repo_manager=env.activations,
            limit=1,
            search_mode="fts",
            rematch=TextRematch("needle", regex=True),
            merge=lambda a, b, k: a + b,
            budget_spent=lambda: False,
        )
        return sizes

    catalog_route = rounds("valid")
    assert rounds("missing") == [1]
    assert catalog_route != [1]
    for state in ("non-object", "dangling", "dirlink"):
        assert rounds(state) == catalog_route, state
