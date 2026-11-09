"""Typed parameters of stored X-Ray patterns: declaration checks, override
resolution and the Rust ``const`` lines injected into the evaluator.

Every name and value reaching generated Rust is validated or escaped here:
names must be Rust identifiers (new stores: upper-case const names),
integers must fit their Rust type, floats must be finite, and strings are
emitted as escaped literals.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

ALLOWED_PARAM_TYPES = frozenset({"usize", "i64", "f64", "bool", "str"})

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONST_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_U64_MAX = 2**64 - 1
_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1
_FIRST_PRINTABLE = 0x20
_DEL = 0x7F


def _matches(rule: "re.Pattern[str]", name: Any) -> bool:
    return isinstance(name, str) and bool(rule.fullmatch(name))


def validate_parameter_declarations(
    params: List[Dict[str, Any]],
    *,
    new_store: bool,
) -> Optional[Dict[str, Any]]:
    """Validate parameter declarations. Returns error dict or None if valid.

    *new_store* applies the upper-case const-name rule (a pattern being
    stored); otherwise any Rust identifier is accepted (a stored pattern
    being loaded).
    """
    rule = _CONST_NAME_RE if new_store else _IDENTIFIER_RE
    for decl in params:
        if not decl.get("name"):
            return {
                "error": "invalid_parameter",
                "message": "Each parameter must have a 'name' field",
            }
        if not _matches(rule, decl["name"]):
            return {
                "error": "invalid_parameter",
                "message": (
                    f"Parameter name {decl['name']!r} must match {rule.pattern}"
                ),
            }
        param_type = decl.get("type", "")
        if param_type not in ALLOWED_PARAM_TYPES:
            return {
                "error": "invalid_parameter_type",
                "message": (
                    f"Parameter type '{param_type}' is not allowed. "
                    f"Allowed types: {sorted(ALLOWED_PARAM_TYPES)}"
                ),
            }
        if "default" not in decl:
            return {
                "error": "invalid_parameter",
                "message": f"Parameter '{decl['name']}' must have a 'default' value",
            }
        try:
            _validate_param_type(decl["name"], param_type, decl["default"])
        except ValueError as exc:
            return {"error": "invalid_parameter", "message": str(exc)}
    return None


def resolve_params(
    decls: List[Dict[str, Any]],
    overrides: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge declared defaults with caller overrides, validating types.

    Raises:
        ValueError: "unknown_parameter" for unrecognized override keys.
        ValueError: "parameter_type_mismatch" for type or range violations.
    """
    declared_names = {d["name"] for d in decls}

    for key in overrides:
        if key not in declared_names:
            raise ValueError(
                f"unknown_parameter: '{key}' is not a declared parameter. "
                f"Declared parameters: {sorted(declared_names)}"
            )

    resolved: Dict[str, Any] = {}
    for decl in decls:
        name = decl["name"]
        param_type = decl["type"]
        default = decl["default"]
        value = overrides.get(name, default)
        _validate_param_type(name, param_type, value)
        resolved[name] = value

    return resolved


def build_const_lines(
    decls: List[Dict[str, Any]],
    resolved: Dict[str, Any],
) -> str:
    """Generate Rust const declarations from resolved parameter values.

    Returns empty string when there are no parameters. Raises ValueError for
    a name that is not a Rust const identifier.
    """
    if not decls:
        return ""

    lines: List[str] = []
    for decl in decls:
        name = decl["name"]
        if not _matches(_IDENTIFIER_RE, name):
            raise ValueError(f"invalid_parameter: parameter name {name!r}")
        param_type = decl["type"]
        value = resolved[name]
        lines.append(_format_const_line(name, param_type, value))

    return "\n".join(lines)


def _mismatch(name: str, expected: str, value: Any) -> ValueError:
    return ValueError(
        f"parameter_type_mismatch: parameter '{name}' expects {expected}, "
        f"got {type(value).__name__} ({value!r})"
    )


def _validate_param_type(name: str, param_type: str, value: Any) -> None:
    """Raise ValueError with 'parameter_type_mismatch' if value doesn't fit type.

    Raises:
        ValueError: With "parameter_type_mismatch" prefix.
    """
    if param_type == "bool":
        if not isinstance(value, bool):
            raise _mismatch(name, "bool", value)
    elif param_type == "str":
        if not isinstance(value, str):
            raise _mismatch(name, "str", value)
    elif param_type in ("usize", "i64"):
        # bool is a subclass of int — reject it explicitly
        if isinstance(value, bool) or not isinstance(value, int):
            raise _mismatch(name, f"{param_type} (integer)", value)
        low, high = (0, _U64_MAX) if param_type == "usize" else (_I64_MIN, _I64_MAX)
        if not low <= value <= high:
            raise _mismatch(name, f"{param_type} in [{low}, {high}]", value)
    elif param_type == "f64":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _mismatch(name, "f64 (number)", value)
        try:
            finite = math.isfinite(value)
        except OverflowError as exc:
            raise _mismatch(name, "a finite f64", value) from exc
        if not finite:
            raise _mismatch(name, "a finite f64", value)


def _rust_string_literal(value: str) -> str:
    """``value`` as a Rust string literal: backslash, double quote and every
    non-printable character escaped (the latter as ``\\u{hex}``)."""
    out = []
    for char in value:
        code = ord(char)
        if char in ('"', "\\"):
            out.append("\\" + char)
        elif code < _FIRST_PRINTABLE or code == _DEL or not char.isprintable():
            out.append("\\u{%x}" % code)
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _format_const_line(name: str, param_type: str, value: Any) -> str:
    """Format a single Rust const declaration line.

    Returns e.g. "const DEPTH_THRESHOLD: usize = 4;"
    """
    rust_type = "&str" if param_type == "str" else param_type

    if param_type == "str":
        rust_value = _rust_string_literal(value)
    elif param_type == "bool":
        rust_value = "true" if value else "false"
    elif param_type == "f64":
        rust_value = repr(float(value))
    else:
        rust_value = str(value)

    return f"const {name}: {rust_type} = {rust_value};"
