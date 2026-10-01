"""Regression tests for Bug #1693.

Bug #1678 fixed `set_xray_executor`/`set_xray_cell_limiter` (xray.py) to probe
the process-wide `code_indexer.server.app` singleton via
`app_module.__dict__.get("app")` instead of a bare `getattr(app_module, "app",
default)`, avoiding the PEP-562 `__getattr__` side effect (Bug #1638) that
permanently constructs and caches the singleton on first touch.

The corresponding READERS were left on the side-effectful `getattr()` form:
  - `_get_xray_executor` (xray.py)
  - `_get_xray_cell_limiter` (xray.py)
  - `_get_xray_cell_limiter` (xray_batch.py)

This module reproduces both consequences described in #1693:
  1. Calling a reader while the singleton is genuinely unbound still
     constructs it as a side effect (the same leak class #1678 fixed for the
     setters).
  2. `_get_xray_executor` raises a misleading
     "set_xray_executor() was not called during startup" message in that
     situation, even though the setter WAS called (it just correctly
     no-op'd per #1678) -- the accurate message is "app is not configured".
"""

from __future__ import annotations

import contextlib
import importlib
import sys
from typing import Any, Generator
from unittest.mock import patch

import pytest


@contextlib.contextmanager
def _fresh_server_app_module() -> Generator:
    """Yield a pristine, never-yet-accessed `code_indexer.server.app` module.

    Pops it from `sys.modules` (and clears the parent package's cached
    attribute) before re-importing, restoring both on exit so this never
    leaks import state into later tests. Mirrors the identical helper added
    for Bug #1678 in tests/unit/server/test_registry_factory_cluster.py.
    """
    import code_indexer.server as server_pkg

    _unset = object()
    saved_module = sys.modules.pop("code_indexer.server.app", None)
    saved_pkg_attr = getattr(server_pkg, "app", _unset)
    try:
        yield importlib.import_module("code_indexer.server.app")
    finally:
        sys.modules.pop("code_indexer.server.app", None)
        if saved_module is not None:
            sys.modules["code_indexer.server.app"] = saved_module
        if saved_pkg_attr is _unset:
            if hasattr(server_pkg, "app"):
                delattr(server_pkg, "app")
        else:
            server_pkg.app = saved_pkg_attr


@contextlib.contextmanager
def _probe_context() -> Generator:
    """Yield a fresh, unbound app module patched into the shared `_utils`
    module (the same object both xray.py and xray_batch.py reference as
    `_utils.app_module`), so a reader call under test genuinely observes an
    unbound singleton regardless of which of the two handler modules it
    lives in.
    """
    import code_indexer.server.mcp.handlers._utils as utils_module

    with _fresh_server_app_module() as fresh_module:
        with patch.object(utils_module, "app_module", fresh_module):
            yield fresh_module


def _assert_singleton_untouched(fresh_module: Any) -> None:
    """Assert the probe never constructed/populated the lazy `app` singleton."""
    assert fresh_module._initialized is False, (
        "Bug #1693: reader probe must not construct the process-wide app singleton"
    )
    assert "app" not in fresh_module.__dict__, (
        "Bug #1693: reader probe must not populate the lazy `app` singleton"
    )


def test_get_xray_executor_probe_does_not_construct_singleton_1693() -> None:
    """_get_xray_executor() must not construct the process-wide app
    singleton when it is genuinely unbound, and must raise an accurate
    "app is not configured" error rather than implying the setter was
    never called."""
    from code_indexer.server.mcp.handlers import xray as xray_module

    with _probe_context() as fresh_module:
        with pytest.raises(RuntimeError) as exc_info:
            xray_module._get_xray_executor()
        _assert_singleton_untouched(fresh_module)

    message = str(exc_info.value)
    assert "app is not configured" in message, (
        f"Bug #1693: error message must accurately state the app is not "
        f"configured: {message}"
    )
    assert "set_xray_executor() was not called" not in message, (
        "Bug #1693: misleading error message -- the setter WAS called, it "
        f"correctly no-op'd per #1678: {message}"
    )


def test_get_xray_cell_limiter_probe_does_not_construct_singleton_1693() -> None:
    """xray.py's _get_xray_cell_limiter() must not construct the
    process-wide app singleton when it is genuinely unbound."""
    from code_indexer.server.mcp.handlers import xray as xray_module

    with _probe_context() as fresh_module:
        result = xray_module._get_xray_cell_limiter()
        _assert_singleton_untouched(fresh_module)

    assert result is None


def test_xray_batch_get_xray_cell_limiter_probe_does_not_construct_singleton_1693() -> (
    None
):
    """xray_batch.py's _get_xray_cell_limiter() must not construct the
    process-wide app singleton when it is genuinely unbound."""
    from code_indexer.server.mcp.handlers import xray_batch as xray_batch_module

    with _probe_context() as fresh_module:
        result = xray_batch_module._get_xray_cell_limiter()
        _assert_singleton_untouched(fresh_module)

    assert result is None
