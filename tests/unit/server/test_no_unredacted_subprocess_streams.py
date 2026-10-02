"""Captured subprocess streams must not reach a message unredacted.

A PAT travels in the clone URL in the default deployment (`LocalGitBackend`
does not own credentials, so `golden_repo_manager._strip_credentials_if_delegated`
is a documented no-op there), which means git prints it back in
``fatal: unable to access 'https://oauth2:<PAT>@host/...'`` on exactly the
failures operators are told to investigate.

``utils/subprocess_diagnostics`` redacts at the formatting boundary, so every
caller of those formatters is safe. This guard is for the OTHER shape: code
that interpolates ``e.stderr`` / ``result.stderr`` straight into an f-string or
a raised error, bypassing the formatter entirely. Seven such sites existed when
this was written, two of them egressing to an HTTP response body and a job
error field.

Written as a static check rather than seven unit tests for the same reason the
git-seam guard is: the defect is a CLASS, and an upstream snapshot can
reintroduce it in code that merges without conflict. Patching the seven shapes
one at a time is what let this one spread.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

_SRC = Path(__file__).resolve().parents[3] / "src" / "code_indexer"
_GOVERNED = ("server", "global_repos")
_EXEMPT_DIRS = ("auto_update", "scripts")

# Attributes that carry raw captured subprocess output.
_STREAM_ATTRS = {"stderr", "stdout"}

# Calls that redact before formatting. Passing a stream to one of these is safe.
_REDACTING = {
    "redact_url_credentials",
    "format_subprocess_failure_diagnostic",
    "format_called_process_error_diagnostic",
    "format_completed_process_diagnostic",
    "mask_url_credentials",
}

ALLOWED: set = set()


def _governed_files() -> List[Path]:
    out: List[Path] = []
    for top in _GOVERNED:
        root = _SRC / top
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if any(part in _EXEMPT_DIRS for part in path.parts):
                continue
            out.append(path)
    return out


def _redacted_somewhere(node: ast.AST) -> bool:
    """True if this expression passes through a redacting call."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute) else None
            )
            if name in _REDACTING:
                return True
    return False


def _scan_source(rel: str, source: str) -> List[str]:
    """Interpolations of a raw captured stream into a message."""
    found: List[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        if _redacted_somewhere(node):
            continue
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            for sub in ast.walk(part.value):
                if (
                    isinstance(sub, ast.Attribute)
                    and sub.attr in _STREAM_ATTRS
                    and not _redacted_somewhere(part.value)
                ):
                    found.append(
                        f"{rel}:{getattr(node, 'lineno', 0)}  "
                        f"raw .{sub.attr} interpolated into a message"
                    )
                    break
    return found


def _scan() -> List[str]:
    out: List[str] = []
    for path in _governed_files():
        rel = str(path.relative_to(_SRC))
        if rel in ALLOWED:
            continue
        out.extend(_scan_source(rel, path.read_text(encoding="utf-8")))
    return out


def test_no_raw_subprocess_stream_in_a_message() -> None:
    leaks = _scan()
    assert not leaks, (
        "these interpolate raw captured subprocess output into a message. git "
        "echoes the credentialed URL back on auth failures, so this is how a "
        "PAT reaches logs, job errors and API responses. Route the stream "
        "through redact_url_credentials() or one of the "
        "subprocess_diagnostics formatters:\n\n"
        + "\n".join(f"  - {x}" for x in leaks)
    )


class TestGuardItself:
    def test_a_raw_stream_is_caught(self) -> None:
        src = 'def f(e):\n    raise RuntimeError(f"failed: {e.stderr}")\n'
        assert _scan_source("server/x.py", src)

    def test_a_redacted_stream_is_accepted(self) -> None:
        src = (
            "def f(e):\n"
            '    raise RuntimeError(f"failed: {redact_url_credentials(e.stderr)}")\n'
        )
        assert not _scan_source("server/x.py", src)

    def test_an_unrelated_attribute_is_ignored(self) -> None:
        src = 'def f(e):\n    raise RuntimeError(f"failed: {e.returncode}")\n'
        assert not _scan_source("server/x.py", src)
