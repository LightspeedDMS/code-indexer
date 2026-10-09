"""``isolated_app`` must not leak a throwaway ``create_app()`` into the process.

A throwaway ``create_app()`` must not (a) construct ``code_indexer.server.app``'s
lazy shared app (PEP 562) as a side effect -- the next file's
``from code_indexer.server.app import app`` would then get a stale app built
over the throwaway root whose auth dependencies were reset to ``None``
(``/auth/login`` 200, ``/mcp`` 401) -- nor (b) leave any module-level binding,
singleton instance state or class-level singleton pointing at the throwaway
server home, nor (c) let a lazy ``server.app`` read during the body build a
SECOND app.

Two cycles: the FIRST observes the lazy-singleton flags (only the first
``create_app()`` of a process can construct it), makes a ``/mcp`` request in
its body, and is then scanned for any state still pointing into its root;
the SECOND runs with every module ``create_app()`` imports already imported,
so every binding has a pre-call value to compare with.  State is read with
``vars()`` so reading never triggers the lazy construction.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import PurePath
from types import ModuleType
from typing import Any, Dict, List, Set, Tuple

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import UserRole
from tests.unit.server._isolated_app import ProcessState, isolated_app

_ABSENT = object()
_APP = "code_indexer.server.app"
_PASSWORD = "Example-Isolated-App-Passw0rd!"
_SCAN_DEPTH = 4

# (module, attribute) bindings create_app()/service_init assign.
_NAMED_BINDINGS: Tuple[Tuple[str, str], ...] = (
    ("code_indexer.server.auth.dependencies", "jwt_manager"),
    ("code_indexer.server.auth.dependencies", "user_manager"),
    ("code_indexer.server.auth.dependencies", "oauth_manager"),
    ("code_indexer.server.auth.dependencies", "mcp_credential_manager"),
    ("code_indexer.server.auth.dependencies", "server_config"),
    ("code_indexer.server.auth.dependencies", "api_key_manager"),
    ("code_indexer.server.web.auth", "_session_manager"),
    (_APP, "jwt_manager"),
    (_APP, "user_manager"),
    ("code_indexer.server.app_helpers", "_server_start_time"),
    ("code_indexer.server.auth.oidc.state_manager", "_configured_sqlite_path"),
    ("code_indexer.server.routers.repo_categories", "_category_service"),
    ("code_indexer.server.services.dependency_latency_tracker", "_tracker_instance"),
    ("code_indexer.server.services.memory_governor", "_governor"),
    (
        "code_indexer.server.services.xray_graph_governor.cache_proxy",
        "_xray_graph_cache",
    ),
    (
        "code_indexer.server.services.xray_graph_governor.k_calibration_store",
        "_xray_k_provider",
    ),
)

# Module-level singletons service_init re-points in place (set_sqlite_path,
# set_backend): their whole instance state must come back.
_SINGLETON_INSTANCES: Tuple[Tuple[str, str], ...] = (
    (_APP, "_token_blacklist"),
    ("code_indexer.server.auth.elevated_session_manager", "elevated_session_manager"),
    ("code_indexer.server.auth.login_rate_limiter", "login_rate_limiter"),
    ("code_indexer.server.auth.session_manager", "session_manager"),
)


def _owned(module_name: Any) -> bool:
    return isinstance(module_name, str) and module_name.startswith("code_indexer")


def _module_attr(module: str, name: str) -> Any:
    mod = sys.modules.get(module)
    return _ABSENT if mod is None else vars(mod).get(name, _ABSENT)


def _lazy_state() -> Dict[str, Any]:
    app_vars = vars(sys.modules[_APP])
    return {
        "_initialized": app_vars["_initialized"],
        "_initializing": app_vars["_initializing"],
        "app": app_vars.get("app", _ABSENT),
        "_lazy_values": dict(app_vars["_lazy_values"]),
    }


def _named_state() -> Dict[str, Any]:
    state: Dict[str, Any] = {f"{m}.{n}": _module_attr(m, n) for m, n in _NAMED_BINDINGS}
    for module, name in _SINGLETON_INSTANCES:
        obj = _module_attr(module, name)
        key = f"{module}.{name}.__dict__"
        state[key] = _ABSENT if obj is _ABSENT else dict(vars(obj))
    from code_indexer.server.services.mcp_self_registration_service import (
        MCPSelfRegistrationService,
    )

    state["MCPSelfRegistrationService._instance"] = vars(
        MCPSelfRegistrationService
    ).get("_instance", _ABSENT)
    return state


def _same(a: Any, b: Any) -> bool:
    """Identity, element-wise for the dict snapshots taken above."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(a[k] is b[k] for k in a)
    return a is b


def _children(value: Any) -> List[Tuple[str, Any]]:
    """What the root scan descends into: code_indexer classes/instances and
    plain containers; everything else is a leaf."""
    if isinstance(value, dict):
        return [(repr(k), v) for k, v in value.items()] + [("<key>", k) for k in value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [("[]", v) for v in value]
    if isinstance(value, type):
        owned = _owned(value.__module__)
        return list(vars(value).items()) if owned else []
    if _owned(type(value).__module__) and isinstance(
        getattr(value, "__dict__", None), dict
    ):
        return list(vars(value).items())
    return []


def _references_to(root: str) -> List[str]:
    """Every str/path reachable (depth-bounded) from an imported code_indexer
    module's globals that still points into *root*."""
    hits: List[str] = []
    seen: Set[int] = set()
    pending: List[Tuple[str, Any, int]] = [
        (f"{name}:{attr}", value, _SCAN_DEPTH)
        for name, module in sorted(sys.modules.items())
        if _owned(name) and isinstance(module, ModuleType)
        for attr, value in vars(module).items()
        if not attr.startswith("__")
    ]
    while pending:  # bounded: every object is expanded once, depth-limited
        label, value, depth = pending.pop()
        if isinstance(value, (str, PurePath)):
            if root in str(value):
                hits.append(label)
            continue
        if depth == 0 or id(value) in seen:
            continue
        seen.add(id(value))
        for key, child in _children(value):
            if not (isinstance(key, str) and key.startswith("__")):
                pending.append((f"{label}.{key}", child, depth - 1))
    return sorted(hits)


def _mcp_round_trip(app: Any) -> int:
    """Log a fresh account in on *app* and POST /mcp tools/list."""
    name = f"isolated-{uuid.uuid4().hex[:8]}"
    app.state.user_manager.create_user(name, _PASSWORD, UserRole.NORMAL_USER)
    client = TestClient(app)
    login = client.post("/auth/login", json={"username": name, "password": _PASSWORD})
    assert login.status_code == 200, login.text[:200]
    token = login.json()["access_token"]
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    return int(resp.status_code)


@pytest.fixture(scope="module")
def cycles(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    from code_indexer.server import app as app_module

    calls: List[int] = []
    real_create_app = app_module.create_app

    def counting_create_app() -> Any:
        calls.append(1)
        return real_create_app()

    counter = pytest.MonkeyPatch()
    counter.setattr(app_module, "create_app", counting_create_app)
    lazy_before = _lazy_state()
    root_1 = tmp_path_factory.mktemp("isolated-cycle-1")
    try:
        with isolated_app(root_1) as app:
            mcp_status = _mcp_round_trip(app)
            body = {
                "create_app_calls": len(calls),
                "mcp_status": mcp_status,
                "process_app_is_throwaway": vars(app_module).get("app") is app,
            }
    finally:
        counter.undo()
    lazy_after = _lazy_state()
    root_references = _references_to(str(root_1))

    named_before = _named_state()
    modules_before = set(sys.modules)
    guard = ProcessState.capture()  # independent of the helper's own snapshot
    capture_imported = set(sys.modules) - modules_before
    with isolated_app(tmp_path_factory.mktemp("isolated-cycle-2")):
        named_inside = _named_state()
        guard_inside = guard.differences()
    return {
        "lazy_before": lazy_before,
        "lazy_after": lazy_after,
        "body": body,
        "root_references": root_references,
        "named_before": named_before,
        "named_inside": named_inside,
        "named_after": _named_state(),
        "guard_inside": guard_inside,
        "guard_after": guard.differences(),
        "capture_imported": sorted(capture_imported),
    }


@pytest.mark.parametrize(
    "key", ["_initialized", "_initializing", "app", "_lazy_values"]
)
def test_isolated_app_leaves_lazy_shared_app_untouched(cycles, key):
    before, after = cycles["lazy_before"][key], cycles["lazy_after"][key]
    assert _same(before, after), f"server.app {key}: {before!r} -> {after!r}"


def test_mcp_request_in_body_builds_no_second_app(cycles):
    """A /mcp request reads ``server.app.app`` (the tool-access memo); during
    the body that must be the throwaway app, never a second create_app()."""
    assert cycles["body"] == {
        "create_app_calls": 1,
        "mcp_status": 200,
        "process_app_is_throwaway": True,
    }


def test_no_state_references_the_throwaway_root_after_exit(cycles):
    """Including singletons built at IMPORT time by modules a first
    create_app() imports while HOME is the throwaway root."""
    assert cycles["root_references"] == []


def test_create_app_really_rebinds_the_named_state(cycles):
    """Discriminator: the restore assertions below are meaningful only if
    create_app() actually changed these bindings while the app was alive."""
    before, inside = cycles["named_before"], cycles["named_inside"]
    changed = [k for k in before if not _same(before[k], inside[k])]
    assert len(changed) >= len(before) // 2, changed


@pytest.mark.parametrize(
    "key",
    [f"{m}.{n}" for m, n in _NAMED_BINDINGS]
    + [f"{m}.{n}.__dict__" for m, n in _SINGLETON_INSTANCES]
    + ["MCPSelfRegistrationService._instance"],
)
def test_isolated_app_restores_binding(cycles, key):
    before, after = cycles["named_before"][key], cycles["named_after"][key]
    assert _same(before, after), f"{key} leaked: {before!r} -> {after!r}"


def test_no_code_indexer_binding_differs_after_isolated_app(cycles):
    """The guard: after undo, no module global, module-level singleton's
    instance attribute, module-level class attribute or module-level
    container in an already-imported code_indexer module differs from its
    pre-create_app() value; while the app was alive they did.

    It compares with the SAME ``ProcessState`` model the helper restores
    with, so it cannot see what that model does not record: attributes
    nested deeper than one level, ``__slots__`` objects, or residue in a
    module first imported during create_app() (the root scan above covers
    the last)."""
    assert "code_indexer.server.auth.dependencies:jwt_manager" in cycles["guard_inside"]
    assert cycles["guard_after"] == []


def test_process_state_capture_imports_nothing(cycles):
    assert cycles["capture_imported"] == []


def test_process_state_detects_and_restores_every_binding_kind(monkeypatch):
    name = "code_indexer._process_state_probe"
    probe: Any = ModuleType(name)  # Any: test sets arbitrary module globals
    Holder: Any = type(  # Any: test sets arbitrary class attributes
        "Holder", (), {"__module__": name, "instance": None}
    )
    holder = Holder()
    holder.path = "/original"
    probe.Holder = Holder
    probe.holder = holder
    probe.value = "original"
    probe.mapping = {"k": "v"}
    probe.items = ["a"]
    probe.members = {"a"}
    monkeypatch.setitem(sys.modules, name, probe)

    state = ProcessState.capture()
    probe.value = "rebound"
    probe.added = object()
    holder.path = "/throwaway"
    Holder.instance = holder
    probe.mapping["k2"] = "v2"
    probe.items.append("b")
    probe.members.add("b")
    # A first import binds its submodule on the parent package.  That binding
    # is deliberately neither reported nor removed (keep_added_modules):
    # dropping it breaks ``package.submodule`` while sys.modules keeps it.
    child = ModuleType(name + ".child")
    probe.child = child

    found = {d for d in state.differences() if d.startswith(name)}
    assert found == {
        f"{name}:value",
        f"{name}:added",
        f"{name}.holder:path",
        f"{name}.Holder:instance",
        f"{name}.mapping:<contents>",
        f"{name}.items:<contents>",
        f"{name}.members:<contents>",
    }

    state.restore()
    assert [d for d in state.differences() if d.startswith(name)] == []
    assert probe.value == "original" and not hasattr(probe, "added")
    assert holder.path == "/original" and Holder.instance is None
    assert probe.mapping == {"k": "v"} and probe.items == ["a"]
    assert probe.members == {"a"}
    assert probe.child is child
