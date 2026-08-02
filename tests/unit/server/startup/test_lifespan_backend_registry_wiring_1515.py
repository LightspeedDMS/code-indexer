"""Bug #1515 regression guard: app.state.backend_registry must be set
BEFORE GlobalReposLifecycleManager (RefreshScheduler/GlobalActivator) is
constructed and started in lifespan.py.

Root cause: make_lifespan()'s lifespan() coroutine constructed and started
GlobalReposLifecycleManager (which immediately spawns a background
"golden-repos-reconcile" thread invoking RefreshScheduler.reconcile_golden_repos(),
which lazily resolves RefreshScheduler.registry / .golden_repo_metadata via
registry_factory.resolve_backend_registry_attr()) BEFORE the line that
assigns app.state.backend_registry = backend_registry. Since
resolve_backend_registry_attr() reads app.state.backend_registry to decide
whether cluster (postgres) mode has a shared backend available, this
ordering gap meant that in postgres/cluster mode, the very first
reconciliation pass (and any activation racing startup) resolved against an
empty, per-node SQLite GlobalRegistry/GoldenRepoMetadataBackend instead of
the shared PostgreSQL-backed one -- producing the "storage_mode=postgres but
backend_registry not set; falling back to SQLite" warning and genuine
registry drift.

This is an AST-based source-order guard (not a raw substring search, so a
docstring/comment mentioning either string can never produce a false pass):
it parses lifespan.py and verifies the EARLIEST `app.state.backend_registry
= backend_registry` assignment STATEMENT (matching BOTH the target and the
right-hand side, so `app.state.backend_registry = None` can never be
mistaken for the real wiring statement) appears (by line number) BEFORE the
EARLIEST `GlobalReposLifecycleManager(...)` call STATEMENT. Since
ast.walk() is not guaranteed to visit nodes in source-line order, ALL
matching nodes are collected and the minimum lineno of each set is used.

This test MUST fail before the Bug #1515 fix (assignment currently appears
AFTER construction) and pass after (assignment moved to occur before
construction).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional


_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


def _first_lineno_of_backend_registry_assignment(tree: ast.AST) -> Optional[int]:
    """Earliest line number among all `app.state.backend_registry =
    backend_registry` Assign nodes -- requires BOTH the target
    (app.state.backend_registry) AND the right-hand side (the bare
    `backend_registry` name) to match, so a differently-valued assignment
    (e.g. `app.state.backend_registry = None`) is never mistaken for the
    real wiring statement. ast.walk() does not guarantee source-line order,
    so ALL matches are collected before taking the minimum lineno."""
    linenos = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (
            isinstance(node.value, ast.Name) and node.value.id == "backend_registry"
        ):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "backend_registry"
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "state"
                and isinstance(target.value.value, ast.Name)
                and target.value.value.id == "app"
            ):
                linenos.append(node.lineno)
    return min(linenos) if linenos else None


def _first_lineno_of_lifecycle_manager_call(tree: ast.AST) -> Optional[int]:
    """Earliest line number among all `GlobalReposLifecycleManager(...)`
    Call nodes. ast.walk() does not guarantee source-line order, so ALL
    matches are collected before taking the minimum lineno."""
    linenos = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "GlobalReposLifecycleManager"
    ]
    return min(linenos) if linenos else None


class TestLifespanBackendRegistryOrderingSourceGuard:
    """AST-based guard: backend_registry must be wired to app.state before
    GlobalReposLifecycleManager (RefreshScheduler/GlobalActivator) is built."""

    def test_backend_registry_assigned_before_global_repos_lifecycle_manager_construction(
        self,
    ) -> None:
        """
        Bug #1515: lifespan.py must assign app.state.backend_registry =
        backend_registry BEFORE constructing GlobalReposLifecycleManager(...).

        GlobalReposLifecycleManager.start() immediately spawns a background
        thread that calls RefreshScheduler.reconcile_golden_repos(), which
        lazily resolves the shared PostgreSQL backend via
        app.state.backend_registry. If that assignment happens LATER in the
        same function, cluster mode's first reconciliation pass (and any
        activation racing it) silently falls back to an empty per-node
        SQLite registry.
        """
        tree = ast.parse(_LIFESPAN_PATH.read_text(), filename=str(_LIFESPAN_PATH))

        assign_lineno = _first_lineno_of_backend_registry_assignment(tree)
        construct_lineno = _first_lineno_of_lifecycle_manager_call(tree)

        assert assign_lineno is not None, (
            "Bug #1515: no 'app.state.backend_registry = backend_registry' "
            "assignment statement found in lifespan.py."
        )
        assert construct_lineno is not None, (
            "Bug #1515: no 'GlobalReposLifecycleManager(...)' call found in "
            "lifespan.py."
        )
        assert assign_lineno < construct_lineno, (
            "Bug #1515: app.state.backend_registry is assigned AFTER "
            "GlobalReposLifecycleManager is constructed/started in "
            "lifespan.py. RefreshScheduler's startup reconciliation thread "
            "(and GlobalActivator) can run before app.state.backend_registry "
            "is populated, causing them to silently fall back to a per-node "
            "SQLite registry in cluster (postgres) mode. Move the "
            "'app.state.backend_registry = backend_registry' assignment to "
            "occur before 'GlobalReposLifecycleManager(' is constructed."
        )
