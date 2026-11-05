"""X-Ray, SCIP and the kernel import nothing from ``code_indexer.server``.

Phase 2 exit criterion of plans/designs/modular-packaging-addons.md. Every
import statement counts: module level, ``TYPE_CHECKING`` blocks and function
bodies. Superseded by the import-linter contract once Phase 1 lands.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List

import pytest

_SRC = Path(__file__).resolve().parents[3] / "src"
_SERVER = "code_indexer.server"


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(_SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package_of(path: Path) -> List[str]:
    """The package relative imports in *path* resolve against."""
    package = _module_name(path).split(".")
    if path.name != "__init__.py":
        package.pop()
    return package


def _imported_modules(source: str, package: List[str]) -> Iterator[str]:
    """Every module an import statement in *source* names, relative ones resolved."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join(base + ([node.module] if node.module else []))
            yield module
            yield from (f"{module}.{alias.name}" for alias in node.names)


def _server_imports(package: str) -> List[str]:
    found = []
    for path in sorted((_SRC / package.replace(".", "/")).rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for module in _imported_modules(source, _package_of(path)):
            if module == _SERVER or module.startswith(_SERVER + "."):
                found.append(f"{path.relative_to(_SRC)}: {module}")
    return found


# X-Ray has no package left in the core (phase 4c): cidx_xray is held to "no
# core import at all, except _platform" by the xray-feature-no-core contract.
@pytest.mark.parametrize("package", ["cidx_scip", "code_indexer.kernel"])
def test_package_imports_nothing_from_the_server(package: str) -> None:
    assert _server_imports(package) == []


def test_the_walk_sees_nested_and_relative_imports() -> None:
    """The guard must not pass vacuously: imports inside a function, a
    TYPE_CHECKING block and a relative import are all reported."""
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import code_indexer.server.app\n"
        "def f():\n"
        "    from code_indexer.server.services import file_service\n"
        "def g():\n"
        "    from ..server import app\n"
    )
    package = ["code_indexer", "xray"]
    server = [m for m in _imported_modules(source, package) if m.startswith(_SERVER)]
    assert server == [
        "code_indexer.server.app",
        "code_indexer.server.services",
        "code_indexer.server.services.file_service",
        "code_indexer.server",
        "code_indexer.server.app",
    ]
