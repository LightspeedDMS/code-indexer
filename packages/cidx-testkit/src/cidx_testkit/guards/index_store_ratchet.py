"""The index-store ratchet's engine: count direct index-directory derivations.

A repository's index state lives where the index store (the
``index_artifacts`` seam, ``cidx_sdk.seams.index_store``) says, not at
``<repo>/.code-indexer``. Every string constant naming the index directory as
a path component is a site that still derives the location by hand; the
counts per module are committed (the core's ``index_store_ratchet.json``) and
may only go down. Docstrings do not count.

Generic over a :class:`~cidx_testkit.guards.source_tree.SourceTree`, so a
module moved into another distribution is still counted (keyed
``<package>/<path>``).
"""

from __future__ import annotations

import ast
from typing import Collection, Dict

from cidx_sdk.seams.index_store import INDEX_DIRNAME
from cidx_testkit.guards.source_tree import SourceTree, all_files


def names_the_index_dir(value: str, dirname: str = INDEX_DIRNAME) -> bool:
    """True when some path component of ``value`` IS the index directory.

    Components, not substrings: ``.code-indexer-override.yaml`` is a different
    file. A leading ``!`` (a negated glob) is not part of the path.
    """
    return dirname in value.lstrip("!").split("/")


def _docstring_nodes(tree: ast.Module) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                found.add(id(body[0].value))
    return found


def count_derivations(source: str) -> int:
    """String constants (not docstrings) naming the index directory."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and names_the_index_dir(node.value)
    )


def scan(tree: SourceTree, exempt: Collection[str] = ()) -> Dict[str, int]:
    """Derivations per module key, for every module of *tree* not *exempt*."""
    counts: Dict[str, int] = {}
    for path in all_files(tree):
        key = tree.key(path)
        if key in exempt:
            continue
        n = count_derivations(path.read_text(encoding="utf-8"))
        if n:
            counts[key] = n
    return counts
