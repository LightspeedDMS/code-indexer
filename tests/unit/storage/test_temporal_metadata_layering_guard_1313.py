"""Bug #1313 layering guard.

TemporalMetadataStore and the backend registry live in the CORE layer
(code_indexer.storage) and are imported by the CLI indexing path. The CLI
must NEVER import anything from code_indexer.server.* (the server is a
separate deployment with its own dependencies).

This test verifies, via AST parsing of the actual import statements (NOT a
substring search, which produces false positives on docstrings/comments
mentioning "code_indexer.server" while explaining this very invariant), that
none of the four new Bug #1313 core modules themselves import anything from
code_indexer.server.

Note: a naive `import code_indexer.storage.temporal_metadata_store` (dotted
submodule import) is NOT a valid way to test this in this codebase, because
Python always executes the parent package's __init__.py first, and
code_indexer/storage/__init__.py pulls in filesystem_vector_store.py, which
already has PRE-EXISTING, deliberate, try/except-guarded module-level server
imports (Story #1110/#1293, unrelated to Bug #1313, unmodified by this fix).
That is an orthogonal, already-reviewed pattern -- not a regression introduced
here -- so this guard targets the four new files' own import graph directly.
"""

import ast
from pathlib import Path

_CORE_MODULES = [
    "storage/temporal_metadata_store.py",
    "storage/temporal_metadata_backend.py",
    "storage/temporal_metadata_backend_registry.py",
    "storage/temporal_metadata_sqlite_backend.py",
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _REPO_ROOT / "src" / "code_indexer"


def _imported_module_names(source: str) -> list:
    """Return every dotted module name referenced by import/from-import statements."""
    tree = ast.parse(source)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


class TestStaticSourceScanHasNoServerImports:
    def test_no_core_temporal_module_imports_server_package(self):
        for rel_path in _CORE_MODULES:
            source = (_SRC_ROOT / rel_path).read_text()
            imported = _imported_module_names(source)
            offending = [
                name
                for name in imported
                if name == "code_indexer.server"
                or name.startswith("code_indexer.server.")
            ]
            assert not offending, (
                f"{rel_path} must NEVER import from code_indexer.server "
                f"(core/server layering violation, Bug #1313); found: {offending}"
            )
