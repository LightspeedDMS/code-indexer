"""
Regression guard for Starlette 1.x TemplateResponse signature migration.

Starlette's Jinja2Templates.TemplateResponse historically accepted the OLD
positional signature `TemplateResponse(name, context)` (template name first).
Starlette 1.x drops the old signature entirely (it becomes a hard error, not
just a DeprecationWarning as in current 0.x). The NEW signature
`TemplateResponse(request, name, context)` (request instance first) is valid
on BOTH old and new Starlette, so migrating every call site is a strict,
backward-compatible improvement.

This test statically scans every .py file under src/code_indexer/server/ for
`*.TemplateResponse(...)` call sites and fails if any call's first positional
argument is a string literal (the tell-tale sign of the OLD, unmigrated
signature). Real source-code inspection, not mocks. Excludes NOTHING as a
special case: no old-style call should exist anywhere in the server tree.
"""

import ast
from pathlib import Path

import pytest

_SERVER_ROOT = Path(__file__).parents[4] / "src" / "code_indexer" / "server"


def _find_old_style_template_response_calls(source_path: Path) -> list:
    """Return a list of (lineno, snippet) for old-style TemplateResponse calls.

    Old style: first positional argument to `<obj>.TemplateResponse(...)` is
    a string constant (the template name), instead of a `Request` instance.
    """
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "TemplateResponse":
            continue
        if not node.args:
            # All-kwargs call style (e.g. request=request, name=...) is fine.
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            violations.append((node.lineno, first_arg.value))
    return violations


def _iter_server_python_files():
    return sorted(_SERVER_ROOT.rglob("*.py"))


def test_no_old_style_template_response_calls_in_server_tree():
    """No `TemplateResponse("template.html", context)` call sites remain.

    Every TemplateResponse call in src/code_indexer/server/ must use the
    new, Starlette-1.x-safe signature: TemplateResponse(request, name, ...).
    """
    assert _SERVER_ROOT.is_dir(), f"Expected server root to exist: {_SERVER_ROOT}"

    all_violations = {}
    for py_file in _iter_server_python_files():
        violations = _find_old_style_template_response_calls(py_file)
        if violations:
            all_violations[str(py_file.relative_to(_SERVER_ROOT))] = violations

    if all_violations:
        details = "\n".join(
            f"  {path}: {[(ln, name) for ln, name in v]}"
            for path, v in all_violations.items()
        )
        pytest.fail(
            "Found old-style TemplateResponse(name, context) call sites "
            "(must migrate to TemplateResponse(request, name, context)):\n"
            f"{details}"
        )
