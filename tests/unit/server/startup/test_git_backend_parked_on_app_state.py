"""The configured GitBackend must be parked on app.state, observably.

handlers/_git_seam.py falls back to LocalGitBackend when nothing is parked. That
fallback is correct for solo/CLI and unit tests, but in a delegated deployment it
would be silently wrong: the handlers would read the local clone while every other
git-executing manager used git-mcp. Nothing external could tell the difference,
because app.state cannot be inspected from outside the process.
"""

from __future__ import annotations

import ast
import inspect


def _lifespan_tree() -> ast.Module:
    from code_indexer.server.startup import lifespan

    return ast.parse(inspect.getsource(lifespan))


def test_the_backend_is_parked_on_app_state() -> None:
    """Without this assignment the handlers silently use a different backend."""
    tree = _lifespan_tree()
    parked = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and target.attr == "git_backend"
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "state"
    ]
    assert parked, "app.state.git_backend is never assigned in lifespan"


def test_the_parking_is_logged_so_it_can_be_verified_externally() -> None:
    """The seam's liveness must be checkable from logs.

    Construction is already logged by git_backend_wiring; that proves a backend was
    built, not that the handlers received it. Only a log at the parking site
    distinguishes "delegated and wired" from "delegated but handlers fell back".
    """
    src = inspect.getsource(
        __import__("code_indexer.server.startup.lifespan", fromlist=["x"])
    )
    tree = ast.parse(src)

    logged = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "info"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
        and "parked on app.state" in call.args[0].value
    ]
    assert logged, "the parked backend is never logged; the seam stays unverifiable"


def test_the_log_reports_which_backend_and_whether_it_owns_credentials() -> None:
    """A bare 'parked' line would not distinguish GitMcpBackend from LocalGitBackend,
    which is the whole question being answered."""
    src = inspect.getsource(
        __import__("code_indexer.server.startup.lifespan", fromlist=["x"])
    )
    tree = ast.parse(src)
    for call in ast.walk(tree):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "info"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
            and "parked on app.state" in call.args[0].value
        ):
            assert "owns_credentials" in call.args[0].value
            # the class name must be interpolated, not hardcoded
            assert len(call.args) >= 2
            return
    raise AssertionError("no parking log call found")
