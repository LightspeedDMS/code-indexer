"""YAML scalar safety helpers for hand-rolled frontmatter emission.

Story #885 A9b: the CIDX lifecycle/dep-map pipeline has several hand-rolled
YAML emitter sites that produce frontmatter by string interpolation
(e.g., f"  - {alias}\n") rather than via yaml.dump. When a scalar starts
with a YAML reserved indicator character (@ ` ! & * ? | > % # or a flow-
collection delimiter), the bare emitted form breaks yaml.safe_load with
ScannerError -- poisoning the entire metadata file. Scoped npm package
names like "@org/pkg" are the most common real-world trigger.

The helper below wraps unsafe scalars in double quotes using YAML's
double-quoted style (which allows the full Unicode range with standard
escapes for \\ and ").

Callers should wrap every interpolated scalar that might not be a plain
alphanumeric identifier.
"""

import re
from typing import Any

# YAML 1.1 reserved indicators that cannot start a plain (unquoted) scalar.
# Flow-collection delimiters { } [ ] , also need quoting when they appear
# anywhere in the scalar (not just at the start), but for START-only checks
# this regex is sufficient for the 99% case our emitters produce.
_YAML_UNSAFE_START = re.compile(r"^[@`!&*?|>%#{}\[\],]")


def yaml_quote_if_unsafe(value: Any) -> str:
    """Return value as a YAML-safe scalar string.

    If the scalar starts with a reserved indicator, contains a colon followed
    by a space (breaks plain-scalar interpretation), or contains a newline,
    wrap it in double quotes with proper escaping. Otherwise return the
    value unchanged (coerced to str).

    Note: this function does NOT attempt to detect already-quoted scalars.
    Call sites are responsible for not passing pre-quoted strings to avoid
    double-quoting. In practice all call sites pass raw repo alias strings
    that are never pre-quoted.

    Examples:
        yaml_quote_if_unsafe("plain-name")    -> "plain-name"
        yaml_quote_if_unsafe("@scope/pkg")    -> '"@scope/pkg"'
        yaml_quote_if_unsafe("key: value")    -> '"key: value"'
    """
    s = str(value)
    if not s:
        return s
    needs_quote = (
        _YAML_UNSAFE_START.match(s) is not None
        or ": " in s
        or "\n" in s
    )
    if not needs_quote:
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
