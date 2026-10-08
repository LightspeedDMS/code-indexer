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
    """Parking a bare LocalRipgrepBackend would ignore the operator's config.

    The assignment existing is not enough -- it has to be the CONFIGURED
    selection, or setting ripgrep_backend='delegated' would change nothing while
    looking wired.
    """
    tree = _lifespan_tree()
    sources = []
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
                sources.append(node.value)

    assert sources, "no app.state.ripgrep_backend assignment found"
    names = {ast.dump(src) for src in sources}
    builder_used = any(
        "build_configured_ripgrep_backend" in ast.dump(src) for src in sources
    ) or any("configured_ripgrep_backend" in n for n in names)
    assert builder_used, (
        "app.state.ripgrep_backend is not assigned from the configured builder, "
        "so ripgrep_backend='delegated' would be ignored"
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
