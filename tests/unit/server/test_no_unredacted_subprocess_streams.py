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
from typing import List, Optional

_SRC = Path(__file__).resolve().parents[3] / "src" / "code_indexer"
_GOVERNED = ("server", "global_repos")
_EXEMPT_DIRS = ("auto_update", "scripts")

# Attributes that carry raw captured subprocess output.
_STREAM_ATTRS = {"stderr", "stdout"}

# Calls that redact before formatting. Passing a stream to one of these is safe.
_REDACTING = {
    "redact_url_credentials",
    "_redact_bytes",
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


_LOG_METHODS = {
    "debug",
    "info",
    "warning",
    "warn",
    "error",
    "exception",
    "critical",
    "log",
}


def _callee_name(fn: ast.AST) -> Optional[str]:
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _unredacted_streams(node: ast.AST) -> List[ast.Attribute]:
    """Stream attributes in *node* that are NOT inside a redacting call.

    Decided per expression, not per statement. The first version asked whether
    a redacting call appeared ANYWHERE in the enclosing f-string, so
    ``f"{redact_url_credentials(cmd)}: {e.stderr}"`` -- argv redacted, stderr
    raw, which is precisely the mixed shape the redaction commit produced --
    cleared the whole string.
    """
    found: List[ast.Attribute] = []

    def visit(n: ast.AST) -> None:
        if isinstance(n, ast.Call) and _callee_name(n.func) in _REDACTING:
            return  # everything inside is redacted
        if isinstance(n, ast.Attribute) and n.attr in _STREAM_ATTRS:
            found.append(n)
            return
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(node)
    return found


def _message_expressions(tree: ast.Module):
    """Expressions whose value becomes operator- or user-visible text.

    Reading a stream is not leaking it -- `if e.stderr:` and
    `e.stderr.strip()` are fine -- so the guard looks at the SINKS rather than
    at every attribute access. Covering only f-strings left
    `logger.error("failed: %s", e.stderr)` invisible, which is the most common
    logging idiom in this codebase.
    """
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            for part in n.values:
                if isinstance(part, ast.FormattedValue):
                    yield part.value
        elif isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Mod)):
            yield n  # "x: " + e.stderr   and   "x: %s" % e.stderr
        elif isinstance(n, ast.Call):
            name = _callee_name(n.func)
            if name in _LOG_METHODS or name == "format":
                for arg in list(n.args) + [kw.value for kw in n.keywords]:
                    yield arg
        elif isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call):
            for arg in list(n.exc.args) + [kw.value for kw in n.exc.keywords]:
                yield arg


def _scan_source(rel: str, source: str) -> List[str]:
    """Raw captured streams reaching a message in *source*."""
    found: List[str] = []
    seen: set = set()
    tree = ast.parse(source)
    for expr in _message_expressions(tree):
        for attr in _unredacted_streams(expr):
            key = (attr.lineno, attr.col_offset)
            if key in seen:
                continue
            seen.add(key)
            found.append(f"{rel}:{attr.lineno}  raw .{attr.attr} reaches a message")
    return sorted(found)


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
        "subprocess_diagnostics formatters:\n\n" + "\n".join(f"  - {x}" for x in leaks)
    )


class TestGuardCoversTheWholeClass:
    """The shapes the first version of this guard could not see.

    It inspected only f-strings, and skipped an entire f-string as soon as ANY
    part of it passed through a redacting call -- so the mixed form, which is
    exactly what the redaction commit produced, sailed through. And
    `logger.error("failed: %s", e.stderr)` is the most common logging idiom in
    this codebase and was wholly invisible. A guard whose docstring claims to
    close a CLASS has to be measured against the class, not against the one
    shape its author happened to write.
    """

    def test_a_mixed_f_string_is_not_cleared_by_its_redacted_half(self) -> None:
        src = (
            "def f(e, cmd):\n"
            '    raise RuntimeError(f"{redact_url_credentials(cmd)}: {e.stderr}")\n'
        )
        assert _scan_source("server/x.py", src), "redacted argv cleared raw stderr"

    def test_percent_style_logging_is_seen(self) -> None:
        for src in (
            'def f(e):\n    logger.error("clone failed: %s", e.stderr)\n',
            'def f(e):\n    logger.warning("out=%s err=%s", e.stdout, e.stderr)\n',
            'def f(e):\n    log.exception("failed: %s", e.stderr)\n',
        ):
            assert _scan_source("server/x.py", src), f"missed:\n{src}"

    def test_concatenation_format_and_direct_raise_are_seen(self) -> None:
        for src in (
            'def f(e):\n    raise RuntimeError("x: " + e.stderr)\n',
            'def f(e):\n    raise RuntimeError("x: {}".format(e.stderr))\n',
            'def f(e):\n    raise RuntimeError("x: %s" % e.stderr)\n',
            "def f(r):\n    raise KeyGenerationError(r.stderr)\n",
        ):
            assert _scan_source("server/x.py", src), f"missed:\n{src}"

    def test_redacted_forms_of_the_same_shapes_are_accepted(self) -> None:
        """Every shape above must go quiet once redacted, or the guard just
        forces people to silence it."""
        for src in (
            'def f(e):\n    logger.error("failed: %s", redact_url_credentials(e.stderr))\n',
            'def f(e):\n    raise RuntimeError("x: " + redact_url_credentials(e.stderr))\n',
            "def f(r):\n    raise KeyGenerationError(redact_url_credentials(r.stderr))\n",
            'def f(e, cmd):\n    raise RuntimeError(f"{cmd}: {redact_url_credentials(e.stderr)}")\n',
        ):
            assert not _scan_source("server/x.py", src), f"false positive:\n{src}"

    def test_non_message_uses_are_not_flagged(self) -> None:
        """Reading a stream is not leaking it; only putting it in a message is.
        Flagging every `.stderr` would make the guard unusable."""
        for src in (
            "def f(e):\n    if e.stderr:\n        pass\n",
            "def f(e):\n    data = e.stderr.strip()\n    return len(data)\n",
            "def f(e):\n    return e.stderr.decode()\n",
        ):
            assert not _scan_source("server/x.py", src), f"false positive:\n{src}"


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
