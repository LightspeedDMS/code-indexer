"""A throwaway real app (``create_app``) over an isolated server home whose
process-wide bindings are put back on exit.

``create_app()`` binds process-wide state as a side effect -- correct for
production (one app per process), a hazard for a test that builds its own
app.  Rather than a hand list that silently misses the next new global,
``ProcessState`` snapshots every already-imported ``code_indexer`` module
before ``create_app()`` runs and ``restore()`` puts back:

* each module global (module ``__dict__`` entries; a submodule binding
  added by a first import is kept -- dropping it would break
  ``package.submodule`` attribute access while ``sys.modules`` keeps it);
* the instance state of every module-level object whose class lives in
  ``code_indexer`` (``set_sqlite_path()`` / ``set_backend()`` re-point
  singletons in place);
* the attributes of every ``code_indexer`` class bound at module level
  (class-level singletons such as ``set_instance()``);
* the contents of module-level ``dict`` / ``list`` / ``set`` objects, and
  of those held as attributes of the instances and classes above
  (registries such as ``DatabaseConnectionManager._instances``).

Only modules already in ``sys.modules`` are walked; ``capture()`` imports
nothing.  Before capturing, ``isolated_app`` imports the modules
``create_app()`` itself imports first, so their import-time singletons are
built under the caller's HOME, not the throwaway one.

Limitation: a module first imported LATER, inside ``create_app()`` (a
function-local import), has no pre-call state; its import-time singletons
keep whatever the throwaway HOME gave them after exit.

During the body the throwaway app IS the process app, installed exactly as
``server.app._ensure_initialized`` installs one in production (``app``,
``_initialized``, ``_lazy_values``), so a lazy ``server.app`` read -- e.g.
the MCP tool-access memo -- returns it instead of building a second app.
``create_app()`` itself runs under ``_lazy_init_lock`` with ``_initializing``
set, as in production.  All of it is reverted on exit.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterator, List, Set, Tuple

import pytest

_ABSENT = object()
_PACKAGE = "code_indexer"


def _owned(module_name: Any) -> bool:
    return isinstance(module_name, str) and (
        module_name == _PACKAGE or module_name.startswith(_PACKAGE + ".")
    )


def _changed_keys(
    live: Any, snap: Dict[str, Any], keep_added_modules: bool
) -> List[Any]:
    """Keys of mapping *live* whose binding differs (by identity) from *snap*
    (dunder names excluded: interpreter bookkeeping, never app state)."""
    changed = []
    for key in set(live) | set(snap):
        if isinstance(key, str) and key.startswith("__") and key.endswith("__"):
            continue
        now, then = live.get(key, _ABSENT), snap.get(key, _ABSENT)
        if now is then:
            continue
        if keep_added_modules and (
            isinstance(now, ModuleType) or isinstance(then, ModuleType)
        ):
            continue
        changed.append(key)
    return sorted(changed, key=repr)


def _container_changed(live: Any, snap: Any) -> bool:
    if isinstance(live, dict):
        return live.keys() != snap.keys() or any(live[k] is not snap[k] for k in snap)
    if isinstance(live, list):
        return len(live) != len(snap) or any(a is not b for a, b in zip(live, snap))
    return bool(live != snap)


class ProcessState:
    """Restorable snapshot of process-wide ``code_indexer`` state."""

    def __init__(self) -> None:
        self._dicts: List[Tuple[str, Dict[str, Any], Dict[str, Any], bool]] = []
        self._classes: List[Tuple[str, type, Dict[str, Any]]] = []
        self._containers: List[Tuple[str, Any, Any]] = []
        self._seen: Set[int] = set()

    @classmethod
    def capture(cls) -> "ProcessState":
        state = cls()
        for name, module in sorted(sys.modules.items()):
            if not _owned(name) or not isinstance(module, ModuleType):
                continue
            live = vars(module)
            state._dicts.append((name, live, dict(live), True))
            for attr, value in list(live.items()):
                if not attr.startswith("__"):
                    state._capture_value(f"{name}.{attr}", value)
        return state

    def _first_sight(self, value: Any) -> bool:
        if id(value) in self._seen:
            return False
        self._seen.add(id(value))
        return True

    def _capture_container(self, label: str, value: Any) -> bool:
        """Record *value*'s contents if it is a dict/list/set; True if it is."""
        if not isinstance(value, (dict, list, set)):
            return False
        if self._first_sight(value):
            copy: Any
            if isinstance(value, dict):
                copy = dict(value)
            elif isinstance(value, list):
                copy = list(value)
            else:
                copy = set(value)
            self._containers.append((label, value, copy))
        return True

    def _capture_value(self, label: str, value: Any) -> None:
        if self._capture_container(label, value) or not self._first_sight(value):
            return
        if isinstance(value, type):
            if not _owned(value.__module__):
                return
            attrs = dict(vars(value))
            self._classes.append((label, value, attrs))
        elif _owned(type(value).__module__) and isinstance(
            getattr(value, "__dict__", None), dict
        ):
            attrs = dict(vars(value))
            self._dicts.append((label, vars(value), attrs, False))
        else:
            return
        for attr, held in attrs.items():  # one level: registries held inside
            if not (isinstance(attr, str) and attr.startswith("__")):
                self._capture_container(f"{label}.{attr}", held)

    def differences(self) -> List[str]:
        """Every binding that differs now from the snapshot (empty = intact)."""
        found = []
        for label, live, snap, is_module in self._dicts:
            found += [f"{label}:{k}" for k in _changed_keys(live, snap, is_module)]
        for label, klass, snap in self._classes:
            found += [f"{label}:{k}" for k in _changed_keys(vars(klass), snap, False)]
        for label, live, snap in self._containers:
            if _container_changed(live, snap):
                found.append(f"{label}:<contents>")
        return found

    def restore(self) -> None:
        for _label, live, snap, is_module in self._dicts:
            for key in _changed_keys(live, snap, is_module):
                if key in snap:
                    live[key] = snap[key]
                else:
                    del live[key]
        for _label, klass, snap in self._classes:
            for key in _changed_keys(vars(klass), snap, False):
                if key in snap:
                    setattr(klass, key, snap[key])
                else:
                    delattr(klass, key)
        for _label, live, snap in self._containers:
            if _container_changed(live, snap):
                if isinstance(live, list):
                    live[:] = snap
                else:
                    live.clear()
                    live.update(snap)


def _import_what_create_app_imports_first() -> None:
    """Modules ``create_app()`` imports anyway, imported BEFORE the snapshot
    so their import-time singletons are built under the caller's HOME and
    restored on exit: the startup modules it imports first, plus the
    function-local imports whose import-time singletons hold server-home
    paths.  ``test_no_state_references_the_throwaway_root_after_exit``
    (test_isolated_app_restores_process_state.py) flags any new one."""
    from code_indexer.server.auth.oidc import state_manager  # noqa: F401
    from code_indexer.server.routers import (  # noqa: F401
        inline_admin_users,
        repo_categories,
    )
    from code_indexer.server.services import (  # noqa: F401
        dependency_latency_tracker,
        file_crud_service,
        mcp_self_registration_service,
        memory_governor,
    )
    from code_indexer.server.services.xray_graph_governor import (  # noqa: F401
        k_calibration_store,
    )
    from code_indexer.server.startup import (  # noqa: F401
        app_wiring,
        lifespan,
        service_init,
    )
    from code_indexer.server.web import auth  # noqa: F401
    from code_indexer.server.wiki import wiki_cache_invalidator  # noqa: F401
    from code_indexer.utils import exception_logger  # noqa: F401


def _install_as_process_app(app_module: Any) -> Any:
    """Run ``create_app()`` and install its app as the process app, exactly
    as ``_ensure_initialized`` does: under ``_lazy_init_lock``, with
    ``_initializing`` set while it runs, so no lazy ``server.app`` read --
    during construction or during the body -- builds a second app."""
    with app_module._lazy_init_lock:
        was_initializing = app_module._initializing
        app_module._initializing = True
        try:
            app = app_module.create_app()
        finally:
            app_module._initializing = was_initializing
        app_module.app = app
        app_module._initialized = True
        for name in app_module._LAZY_INIT_ATTRS:
            # .get mirrors _ensure_initialized's globals().get(n): a lazy
            # name create_app() never binds (langfuse_sync_service) is None.
            app_module._lazy_values[name] = vars(app_module).get(name)
    return app


@contextmanager
def isolated_app(root: Path) -> Iterator[Any]:
    """Yield a real ``create_app()`` app whose server home is under *root*
    (never ~/.cidx-server); on exit restore every process-wide binding the
    app installed, then the environment."""
    from code_indexer.server import app as app_module

    _import_what_create_app_imports_first()
    state = ProcessState.capture()
    patcher = pytest.MonkeyPatch()
    try:
        patcher.setenv("HOME", str(root / "home"))
        patcher.setenv("CIDX_SERVER_DATA_DIR", str(root / "server"))
        app = _install_as_process_app(app_module)
        store = app.state.user_manager._sqlite_backend._conn_manager.db_path
        assert str(store).startswith(str(root)), store
        yield app
    finally:
        try:
            state.restore()
        finally:
            patcher.undo()
