"""Closeout wiring checks not covered by the per-batch guards.

- No door calls the MFA primitives the audited activation / regeneration
  entry points wrap (``activate_mfa``, ``generate_recovery_codes``).
- No audited entry point has an ``"admin"`` default, and the parameter that
  names the actor has no default at all; ``golden_repo_manager.py`` carries
  no ``"admin"`` default anywhere.

The MFA scan reads every door layer recursively (the account wiring guard's
door set plus ``global_routes/``, ``routes/`` and every ``auth/`` route
module).  The syntax-tree helpers are the account wiring guard's
(``test_account_audit_wiring``), so a spaced call, a saved method reference
and a by-name lookup are all caught; the entry-point names are the union of
the three existing guards' lists.

The other closeout wiring checks are the existing guards':
- each door uses the audited entry point, never the primitive:
  ``test_account_audit_wiring`` (accounts, credentials) and
  ``test_repo_config_audit_wiring`` (per door layer, repositories and
  configuration); every mapped door reaching its emission, per route and
  per MCP tool: ``test_catalog_completeness``;
- ``save_config`` outside the allowed writers: ``test_config_single_writer``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, List, Set

import code_indexer
from test_account_audit_wiring import (
    _AUDITED_ENTRY_POINTS as _ACCOUNT_ENTRY_POINTS,
    _DOOR_FILES,
    _attribute_names,
    _name_of,
)
from test_catalog_completeness import _ENTRY_POINTS
from test_repo_config_audit_wiring import (
    _AUDITED_ENTRY_POINTS as _REPO_CONFIG_ENTRY_POINTS,
)

_PACKAGE = Path(code_indexer.__file__).parent
_SERVER = _PACKAGE / "server"
_MFA_PRIMITIVES = frozenset({"activate_mfa", "generate_recovery_codes"})
_MFA_ENTRY_POINTS = frozenset(
    {"activate_mfa_and_issue_recovery_codes", "regenerate_recovery_codes"}
)
# Every door layer, recursively, plus every route module under ``auth/``.
_MFA_DOOR_FILES: List[Path] = sorted(
    {
        *_DOOR_FILES,
        *(
            path
            for layer in ("routers", "web", "mcp/handlers", "global_routes", "routes")
            for path in (_SERVER / layer).rglob("*.py")
        ),
        *(_SERVER / "auth").rglob("*routes*.py"),
    }
)
_ACTOR_PARAMETERS = frozenset({"actor", "submitter_username", "granted_by"})
_AUDITED: Set[str] = (
    set(_ACCOUNT_ENTRY_POINTS)
    | set(_REPO_CONFIG_ENTRY_POINTS)
    | {name for names in _ENTRY_POINTS.values() for name in names}
)


def _mfa_primitive_uses(source: str) -> List[str]:
    return [
        f"{getattr(node, 'lineno', '?')}: {_name_of(node)}"
        for node in _attribute_names(ast.parse(source))
        if _name_of(node) in _MFA_PRIMITIVES
    ]


def _defaults(node: ast.AST) -> Iterable[tuple]:
    """(parameter name, default node) for every defaulted parameter."""
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    tail = positional[len(positional) - len(args.defaults) :]
    yield from ((a.arg, d) for a, d in zip(tail, args.defaults))
    yield from (
        (a.arg, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None
    )


def _default_violations(source: str, names: Set[str], label: str) -> List[str]:
    """``label:line name(param)`` for an admin default or a defaulted actor."""
    found: List[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in names:
            continue
        for param, default in _defaults(node):
            is_admin = isinstance(default, ast.Constant) and default.value == "admin"
            if is_admin or param in _ACTOR_PARAMETERS:
                found.append(f"{label}:{node.lineno} {node.name}({param})")
    return found


def _all_function_names(source: str) -> Set[str]:
    return {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_no_door_calls_an_mfa_primitive() -> None:
    offenders = [
        f"{path.relative_to(_SERVER)}:{use}"
        for path in _MFA_DOOR_FILES
        for use in _mfa_primitive_uses(path.read_text())
    ]
    assert offenders == []


def test_the_mfa_scan_covers_the_mfa_doors() -> None:
    """The scan is not vacuous: it reads the doors that do MFA work."""
    relative = {path.relative_to(_SERVER).as_posix() for path in _MFA_DOOR_FILES}
    assert {"web/mfa_routes.py", "auth/elevation_routes.py"} <= relative
    mfa_door = (_SERVER / "web" / "mfa_routes.py").read_text()
    named = {_name_of(node) for node in _attribute_names(ast.parse(mfa_door))}
    assert _MFA_ENTRY_POINTS <= named


def test_no_audited_entry_point_has_an_admin_or_actor_default() -> None:
    offenders: List[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        offenders.extend(
            _default_violations(
                path.read_text(), _AUDITED, path.relative_to(_PACKAGE).as_posix()
            )
        )
    assert offenders == []


def test_every_listed_entry_point_is_defined_somewhere() -> None:
    """The name list is not stale: each audited entry point still exists."""
    defined: Set[str] = set()
    for path in _PACKAGE.rglob("*.py"):
        defined |= _all_function_names(path.read_text())
    assert sorted(_AUDITED - defined) == []


def test_the_golden_repo_manager_has_no_admin_default_anywhere() -> None:
    path = _SERVER / "repositories" / "golden_repo_manager.py"
    source = path.read_text()
    offenders = [
        f"{node.lineno}: {node.name}({param})"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for param, default in _defaults(node)
        if isinstance(default, ast.Constant) and default.value == "admin"
    ]
    assert offenders == []


def test_the_scans_detect_every_violation_shape() -> None:
    """Negative controls for both scans."""
    assert _mfa_primitive_uses("svc.activate_mfa(u, c)") == ["1: activate_mfa"]
    assert _mfa_primitive_uses("fn = svc.generate_recovery_codes") == [
        "1: generate_recovery_codes"
    ]
    assert _mfa_primitive_uses("getattr(svc, 'activate_mfa')(u, c)") == [
        "1: activate_mfa"
    ]
    assert _mfa_primitive_uses("svc.activate_mfa_and_issue_recovery_codes(u)") == []
    assert _mfa_primitive_uses("svc.regenerate_recovery_codes(u, actor=a)") == []

    names = {"remove_golden_repo", "save_token_audited"}
    assert _default_violations(
        "def remove_golden_repo(self, alias, submitter_username='admin'): pass",
        names,
        "m.py",
    ) == ["m.py:1 remove_golden_repo(submitter_username)"]
    assert _default_violations(
        "def save_token_audited(self, p, t, *, actor=None): pass", names, "m.py"
    ) == ["m.py:1 save_token_audited(actor)"]
    assert _default_violations(
        "async def remove_golden_repo(self, alias, mode='admin', *, "
        "submitter_username): pass",
        names,
        "m.py",
    ) == ["m.py:1 remove_golden_repo(mode)"]
    assert (
        _default_violations(
            "def save_token_audited(self, p, t, base=None, *, actor): pass",
            names,
            "m.py",
        )
        == []
    )
