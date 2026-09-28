"""Door layers reach account and credential changes only through audited paths.

Every front door (REST routers, Web routes, MCP handlers) must use the
audited entry points, never the unaudited primitives they wrap -- otherwise
a door could change an account or credential without its audit row.  The
scan is over each door module's syntax tree, so a spaced call
(``obj.method (``), a saved method reference (``fn = obj.method``) and a
by-name lookup (``getattr(obj, "method")``) are all caught.  Every audited
entry point must also be reachable from a door.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List

import code_indexer

_SERVER = Path(code_indexer.__file__).parent / "server"
_DOOR_FILES: List[Path] = sorted(
    [
        *(_SERVER / "routers").glob("*.py"),
        *(_SERVER / "web").glob("*.py"),
        *(_SERVER / "mcp" / "handlers").rglob("*.py"),
        _SERVER / "auth" / "oidc" / "routes.py",
        _SERVER / "auth" / "oauth" / "routes.py",
    ]
)

_UNAUDITED_PRIMITIVES = frozenset(
    {
        "create_user",
        "delete_user",
        "update_user_role",
        "change_password",
        "update_user",
        "delete_api_key",
        "generate_key",
        "generate_credential",
        "revoke_credential",
        "create_key",
        "delete_key",
        "assign_key_to_host",
        "configure_credential",
        "delete_credential",
    }
)
_AUDITED_ENTRY_POINTS = frozenset(
    {f"{name}_audited" for name in _UNAUDITED_PRIMITIVES if name != "update_user"}
    | {"update_user_email_audited"}
)


def _attribute_names(tree: ast.AST) -> List[ast.AST]:
    """Nodes that name a method: attribute access or a getattr by string."""
    found: List[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            found.append(node)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found.append(node)
    return found


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    assert isinstance(node, ast.Call)
    constant = node.args[1]
    assert isinstance(constant, ast.Constant)
    return str(constant.value)


def _primitive_uses(source: str) -> List[str]:
    return [
        f"{getattr(node, 'lineno', '?')}: {_name_of(node)}"
        for node in _attribute_names(ast.parse(source))
        if _name_of(node) in _UNAUDITED_PRIMITIVES
    ]


def _door_sources() -> Dict[Path, str]:
    return {path: path.read_text() for path in _DOOR_FILES}


def test_no_door_uses_an_unaudited_primitive() -> None:
    offenders = [
        f"{path.relative_to(_SERVER)}:{use}"
        for path, source in _door_sources().items()
        for use in _primitive_uses(source)
    ]
    assert offenders == []


def test_every_audited_entry_point_is_reachable_from_a_door() -> None:
    used = {
        _name_of(node)
        for source in _door_sources().values()
        for node in _attribute_names(ast.parse(source))
    }
    assert sorted(_AUDITED_ENTRY_POINTS - used) == []


def test_the_scan_detects_every_way_of_naming_a_primitive() -> None:
    """Negative controls: each spelling of a primitive use is flagged."""
    assert _primitive_uses("manager.delete_key(name)") == ["1: delete_key"]
    assert _primitive_uses("manager.delete_key (name)") == ["1: delete_key"]
    assert _primitive_uses("fn = manager.delete_key\nfn(name)") == ["1: delete_key"]
    assert _primitive_uses("run(users.delete_user, name)") == ["1: delete_user"]
    assert _primitive_uses("getattr(manager, 'create_key')(n)") == ["1: create_key"]
    assert _primitive_uses("manager.delete_key_audited(name, actor=a)") == []
