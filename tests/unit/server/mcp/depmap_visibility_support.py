"""Shared harness for the catalog visibility tests of the depmap tools and
the quick reference: real fixture directory, real group store, real
registry; only the server's ``app_module`` wiring is a stand-in."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, Tuple

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp import handlers as handlers_package
from code_indexer.server.mcp.handlers import HANDLER_REGISTRY, _utils
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    AUDIT_DOMAIN,
    CANARY,
    GAMMA,
    CidxMetaFixture,
)
from tests.unit.server.mcp.conftest import extract_mcp_data

SIX_CALLS: Dict[str, Tuple[str, Dict[str, Any]]] = {
    "graph": ("depmap_get_cross_domain_graph", {}),
    "hubs": ("depmap_get_hub_domains", {"top_n": 50}),
    "stale": ("depmap_get_stale_domains", {"days_threshold": 0}),
    "summary_payments": ("depmap_get_domain_summary", {"domain_name": "payments"}),
    "summary_audit": ("depmap_get_domain_summary", {"domain_name": AUDIT_DOMAIN}),
    "summary_canary": ("depmap_get_domain_summary", {"domain_name": CANARY}),
    "summary_reports": ("depmap_get_domain_summary", {"domain_name": "reports"}),
    "consumers_alpha": ("depmap_find_consumers", {"repo_name": ALPHA}),
    "repo_domains_alpha": ("depmap_get_repo_domains", {"repo_name": ALPHA}),
}


def make_user(name: str, role: UserRole = UserRole.ADMIN) -> User:
    # Role ADMIN passes role gates; visibility is decided by admins-group
    # membership only.
    return User(
        username=name, password_hash="hash", role=role, created_at=datetime.now()
    )


def build_depmap_server(
    monkeypatch: pytest.MonkeyPatch,
    fx: CidxMetaFixture,
    golden_repo_manager: Any = None,
) -> SimpleNamespace:
    state = SimpleNamespace(
        dependency_map_service=SimpleNamespace(cidx_meta_read_path=fx.root),
        access_filtering_service=fx.access_service,
        backend_registry=SimpleNamespace(global_repos=fx.registry),
    )
    app_module = SimpleNamespace(
        app=SimpleNamespace(state=state), golden_repo_manager=golden_repo_manager
    )
    monkeypatch.setattr(_utils, "app_module", app_module)
    monkeypatch.setattr(handlers_package, "app_module", app_module)
    return state


def call_tool(tool: str, args: Dict[str, Any], username: str) -> Dict[str, Any]:
    result = HANDLER_REGISTRY[tool](dict(args), make_user(username))
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    data: Dict[str, Any] = extract_mcp_data(result)
    return data


def normalize(data: Any, root: Path) -> Any:
    """Tmp paths and day counts replaced, so a recording stays comparable."""
    text = json.dumps(data, sort_keys=True)
    for form in {str(root.resolve()), str(root)}:
        text = text.replace(form, "<root>")
    value = json.loads(text)
    for entry in value.get("stale_domains", []) if isinstance(value, dict) else []:
        entry["days_stale"] = "<n>"
    return value


def assert_hidden_absent(data: Any, extra: Iterable[str] = ()) -> str:
    """The FULL serialized response names neither the canary nor gamma."""
    text = json.dumps(data, sort_keys=True)
    for name in (CANARY, GAMMA, *extra):
        assert name not in text, f"{name!r} leaked: {text}"
    return text
