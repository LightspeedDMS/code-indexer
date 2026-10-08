"""The configured RipgrepBackend must be parked on app.state, observably.

The sibling of ``test_git_backend_parked_on_app_state``. A selection that is
built but never parked is invisible from outside the process: app.state cannot
be inspected remotely, so a delegated deployment whose parking silently failed
would have every consumer fall back to reading local disk -- in a pod that may
have no repository mount, which surfaces as an empty repository rather than an
error.

Source-level because the alternative is booting the whole lifespan, which needs
PostgreSQL, a job manager and a repository tree. These assert the two facts that
actually go wrong: the assignment exists, and the parking is logged so it can be
confirmed from outside.
"""

from __future__ import annotations

import ast
import inspect


def _lifespan_tree() -> ast.Module:
    from code_indexer.server.startup import lifespan

    return ast.parse(inspect.getsource(lifespan))


def test_the_backend_is_parked_on_app_state() -> None:
    """Without this assignment every consumer silently keeps reading local disk."""
    tree = _lifespan_tree()
    parked = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and target.attr == "ripgrep_backend"
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "state"
    ]
    assert parked, "app.state.ripgrep_backend is never assigned in lifespan"


def test_the_parked_value_comes_from_the_configured_builder() -> None:
    """The parked object must be what the BUILDER returned.

    The assignment existing is not enough -- parking a bare
    LocalRipgrepBackend would ignore the operator's config entirely while
    looking wired.

    An earlier version of this test accepted either "the assigned expression
    mentions the builder" OR "the assigned expression mentions a variable with
    the right name". The assignment is a bare Name, so the first branch never
    matched and only the name check ever ran: assigning
    ``app.state.ripgrep_backend = LocalRipgrepBackend()`` to a variable called
    ``configured_ripgrep_backend`` would have passed. The name is now traced back
    to the call that bound it.
    """
    tree = _lifespan_tree()

    parked: list = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "ripgrep_backend"
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "state"
            ):
                parked.append(node.value)
    assert parked, "no app.state.ripgrep_backend assignment found"

    def binds_from_builder(name: str) -> bool:
        """Was `name` bound from a build_configured_ripgrep_backend(...) call?"""
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                continue
            call = node.value
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "build_configured_ripgrep_backend"
            ):
                return True
        return False

    def from_builder(expr: ast.expr) -> bool:
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "build_configured_ripgrep_backend"
        ):
            return True
        return isinstance(expr, ast.Name) and binds_from_builder(expr.id)

    assert any(from_builder(expr) for expr in parked), (
        "app.state.ripgrep_backend is not assigned from "
        "build_configured_ripgrep_backend, so ripgrep_backend='delegated' would be "
        "ignored"
    )


def test_the_parking_is_logged_so_it_can_be_verified_externally() -> None:
    """Construction logging proves a backend was built, not that anyone got it."""
    tree = _lifespan_tree()
    logged = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "info"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
        and "RipgrepBackend parked on app.state" in call.args[0].value
    ]
    assert logged, (
        "the RipgrepBackend parking is not logged, so a deployment cannot tell "
        "whether the seam is live"
    )
