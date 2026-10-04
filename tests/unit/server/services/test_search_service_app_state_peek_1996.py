"""Bug #1996: the search-service app.state accessors must never BUILD the server.

``_get_http_client_factory`` / ``_get_query_executor`` /
``_get_repo_config_cache`` are documented to return None outside the server
lifespan (CLI in-process).  They used ``from ..app import app``, which goes
through app.py's PEP 562 ``__getattr__`` and runs the full ``create_app()``.
The standalone ``cidx query`` rerank path calls ``_get_http_client_factory``,
so every CLI query with results built the whole server in the CLI process:
it took the primary-instance lock, opened every server database and rewrote
``config.json`` / ``launch.json`` in the server home.

A fresh subprocess keeps the interpreter clean (the lazy app is process-wide
state), with HOME and the server data dirs pointed at a temp dir.
"""

from __future__ import annotations

import os
import site
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

import code_indexer.server.app as app_module
from code_indexer.server.services import search_service

SRC_ROOT = str(Path(__file__).resolve().parents[4] / "src")
SUBPROCESS_TIMEOUT_SECONDS = 120

_PROBE = """
import code_indexer.server.app as app_module
from code_indexer.server.services import search_service as ss
print("factory:", ss._get_http_client_factory())
print("executor:", ss._get_query_executor())
print("repo_cache:", ss._get_repo_config_cache())
print("initialized:", app_module._initialized)
print("app_in_dict:", "app" in vars(app_module))
"""


@pytest.mark.timeout(SUBPROCESS_TIMEOUT_SECONDS + 15)
def test_accessors_return_none_without_building_the_app(tmp_path: Path) -> None:
    home = tmp_path / "home"
    server_dir = home / ".cidx-server"
    home.mkdir()
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        CIDX_SERVER_DATA_DIR=str(server_dir),
        CIDX_DATA_DIR=str(server_dir),
        PYTHONPATH=os.pathsep.join([SRC_ROOT, site.getusersitepackages()]),
    )
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "factory: None" in out, out
    assert "executor: None" in out, out
    assert "repo_cache: None" in out, out
    assert "initialized: False" in out, f"the server app was built: {out}"
    assert "app_in_dict: False" in out, out
    created = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert created == [], f"server files created by a non-server process: {created}"


def _live_app() -> tuple:
    """A real FastAPI app whose state carries three distinct live objects."""
    live = FastAPI()
    values = SimpleNamespace(factory=object(), executor=object(), cache=object())
    live.state.http_client_factory = values.factory
    live.state.query_executor = values.executor
    live.state.repo_config_cache = values.cache
    return live, values


def _assert_accessors_return(values: SimpleNamespace) -> None:
    assert search_service._get_http_client_factory() is values.factory
    assert search_service._get_query_executor() is values.executor
    assert search_service._get_repo_config_cache() is values.cache


def test_accessors_return_live_state_of_an_assigned_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 style: the app assigned to the module attribute (never via
    getattr, which would run create_app through __getattr__)."""
    live, values = _live_app()
    monkeypatch.setitem(vars(app_module), "app", live)

    assert app_module.peek_app() is live
    _assert_accessors_return(values)


def test_accessors_return_live_state_of_a_lazily_built_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production uvicorn path: lazy init ran and snapshotted the app; the
    module attribute itself may be absent (e.g. after a patch removed it)."""
    live, values = _live_app()
    monkeypatch.delitem(vars(app_module), "app", raising=False)
    monkeypatch.setattr(app_module, "_initialized", True)
    monkeypatch.setitem(app_module._lazy_values, "app", live)

    assert app_module.peek_app() is live
    _assert_accessors_return(values)
