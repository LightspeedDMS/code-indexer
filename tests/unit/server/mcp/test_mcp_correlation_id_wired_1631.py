"""Bug #1631: MCP handler submodules import get_correlation_id from the
UNWIRED code_indexer.server.middleware.correlation module instead of the
WIRED code_indexer.server.telemetry.correlation_bridge reader.

CorrelationContextMiddleware (which populates the unwired reader's
ContextVar) is never registered in startup/app_wiring.py -- only
CorrelationBridgeMiddleware (which populates the wired reader) is. So
get_correlation_id() in every affected module always returns None in
production, and every audit/diagnostic log line that includes
correlation_id silently carries correlation_id=None.

This exact class of bug was already found and fixed once, for search.py
and the handlers/__init__.py package re-export, under Story #1293 (see
test_mcp_correlation_id_wired_1293.py). This test extends the same
guarantee to the remaining domain submodules (#1631).

SOURCE-LEVEL verification: this test parses each file's AST and inspects
its import statements directly -- it does NOT rely on the live runtime
attribute (module.get_correlation_id). This is deliberate: the handlers
package installs a _ForwardingModule shim whose __setattr__ mirrors
attribute writes across sibling submodules (see bug #1610), which can
mask a wrong import at runtime by making the live attribute look correct
even when the file's own import statement is wrong. Parsing the source
text is independent of that runtime behavior and would have caught this
bug regardless of whether #1610's masking accident existed.
"""

import ast
from pathlib import Path
from typing import Optional

import pytest

import code_indexer

_SRC_ROOT = Path(code_indexer.__file__).resolve().parent.parent
_HANDLERS_DIR = _SRC_ROOT / "code_indexer" / "server" / "mcp" / "handlers"

_WIRED_MODULE = "code_indexer.server.telemetry.correlation_bridge"
_UNWIRED_MODULE = "code_indexer.server.middleware.correlation"

# (relative path under handlers/, dotted __package__ of that file)
_AFFECTED_FILES = [
    ("cicd.py", "code_indexer.server.mcp.handlers"),
    ("files.py", "code_indexer.server.mcp.handlers"),
    ("repos.py", "code_indexer.server.mcp.handlers"),
    ("git_write.py", "code_indexer.server.mcp.handlers"),
    ("guides.py", "code_indexer.server.mcp.handlers"),
    ("pull_requests.py", "code_indexer.server.mcp.handlers"),
    ("scip.py", "code_indexer.server.mcp.handlers"),
    ("git_read.py", "code_indexer.server.mcp.handlers"),
    ("ssh_keys.py", "code_indexer.server.mcp.handlers"),
    ("admin/__init__.py", "code_indexer.server.mcp.handlers.admin"),
    ("_utils.py", "code_indexer.server.mcp.handlers"),
    ("admin/mcp_credentials.py", "code_indexer.server.mcp.handlers.admin"),
]


def _resolve_relative_module(module: Optional[str], level: int, package: str) -> str:
    """Resolve a (possibly relative) ImportFrom node to a dotted module name.

    Mirrors Python's own relative-import resolution: level=1 means the
    current package itself, level=2 means its parent, etc.
    """
    if level == 0:
        assert module is not None
        return module
    parts = package.split(".")
    base_len = len(parts) - level + 1
    assert base_len > 0, f"relative import level {level} escapes package {package!r}"
    base = parts[:base_len]
    if module:
        base.append(module)
    return ".".join(base)


def _find_get_correlation_id_source_module(
    file_path: Path, package: str
) -> Optional[str]:
    """Return the dotted module that binds the name get_correlation_id in
    file_path's import statements (absolute or relative), or None if the
    file never imports a name bound to get_correlation_id."""
    tree = ast.parse(file_path.read_text(), filename=str(file_path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            bound_name = alias.asname or alias.name
            if bound_name == "get_correlation_id":
                return _resolve_relative_module(node.module, node.level, package)
    return None


class TestAllDomainSubmodulesUseWiredCorrelationIdReader:
    """Bug #1631: every listed MCP handler submodule must import
    get_correlation_id from the WIRED telemetry.correlation_bridge reader,
    never from the unwired middleware.correlation module."""

    @pytest.mark.parametrize(
        "relative_path,package",
        _AFFECTED_FILES,
        ids=[f[0] for f in _AFFECTED_FILES],
    )
    def test_source_imports_get_correlation_id_from_wired_module(
        self, relative_path: str, package: str
    ) -> None:
        file_path = _HANDLERS_DIR / relative_path
        assert file_path.exists(), f"expected file to exist: {file_path}"

        resolved_module = _find_get_correlation_id_source_module(file_path, package)

        assert resolved_module is not None, (
            f"{relative_path} does not import a name bound to "
            "get_correlation_id at all -- expected an import from "
            f"{_WIRED_MODULE}"
        )
        assert resolved_module == _WIRED_MODULE, (
            f"{relative_path} imports get_correlation_id from "
            f"{resolved_module!r}, which is the UNWIRED reader whose "
            "CorrelationContextMiddleware is never registered in "
            "startup/app_wiring.py. It must import from the WIRED "
            f"reader instead: {_WIRED_MODULE}."
        )
        assert resolved_module != _UNWIRED_MODULE

    def test_no_affected_file_still_references_unwired_module_at_all(self) -> None:
        """Belt-and-suspenders: the unwired module string must not appear
        anywhere in these files' import statements, even under an alias
        that isn't literally named get_correlation_id."""
        offenders = []
        for relative_path, package in _AFFECTED_FILES:
            file_path = _HANDLERS_DIR / relative_path
            tree = ast.parse(file_path.read_text(), filename=str(file_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                resolved = _resolve_relative_module(node.module, node.level, package)
                if resolved == _UNWIRED_MODULE:
                    offenders.append(relative_path)
        assert offenders == [], (
            f"files still importing from the unwired {_UNWIRED_MODULE}: {offenders}"
        )
