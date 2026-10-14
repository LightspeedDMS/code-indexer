"""Tests for _ForwardingModule and _LegacyForwardingModule (Story #496).

Verify that mock.patch writes on the handlers package and _legacy module
are propagated to all extracted domain submodules, ensuring test patches
work transparently after the handler modularization.
"""

import sys
from contextlib import contextmanager
from types import ModuleType
from typing import Any, Generator, List
from unittest.mock import MagicMock


_ALL_DOMAIN_SUBMODULES = [
    "code_indexer.server.mcp.handlers.scip",
    "code_indexer.server.mcp.handlers.guides",
    "code_indexer.server.mcp.handlers.ssh_keys",
    "code_indexer.server.mcp.handlers.pull_requests",
    "code_indexer.server.mcp.handlers.git_read",
    "code_indexer.server.mcp.handlers.git_write",
    "code_indexer.server.mcp.handlers.admin",
    "code_indexer.server.mcp.handlers.cicd",
    "code_indexer.server.mcp.handlers.files",
    "code_indexer.server.mcp.handlers.repos",
    "code_indexer.server.mcp.handlers.search",
]


def _ensure_modules_loaded() -> None:
    """Import handlers package so all submodules are in sys.modules."""
    import code_indexer.server.mcp.handlers  # noqa: F401
    import code_indexer.server.mcp.handlers._legacy  # noqa: F401


@contextmanager
def _sentinel_across_submodules(
    attr_name: str,
) -> Generator[object, None, None]:
    """Plant a matching "already forwarded" placeholder on the handlers
    package AND in every domain submodule (same object identity), yield a
    distinct sentinel to write next, then clean up all modules.

    Bug #1610: the corrected _ForwardingModule.__setattr__ only propagates a
    package-level write into a submodule when that submodule's CURRENT
    binding for the name is identical to the package's PREVIOUS binding --
    i.e. the name genuinely was a shared/forwarded alias, not merely a
    same-named attribute that happens to exist there.  Planting the SAME
    placeholder object in both the package's own __dict__ and every
    submodule's __dict__ models a real prior-forwarding state, so the
    subsequent `setattr(handlers, attr_name, sentinel)` in the tests below
    is recognized as a legitimate forward and propagates everywhere.
    """
    _ensure_modules_loaded()
    import code_indexer.server.mcp.handlers as handlers

    placeholder = object()
    sentinel = object()
    handlers.__dict__[attr_name] = placeholder
    modules: List[ModuleType] = []
    for mod_name in _ALL_DOMAIN_SUBMODULES:
        mod = sys.modules.get(mod_name)
        assert mod is not None, f"{mod_name} not loaded"
        mod.__dict__[attr_name] = placeholder
        modules.append(mod)
    try:
        yield sentinel
    finally:
        handlers.__dict__.pop(attr_name, None)
        for mod in modules:
            mod.__dict__.pop(attr_name, None)


def _assert_forwarded_to_all(attr_name: str, sentinel: object) -> None:
    """Assert every domain submodule received the sentinel value."""
    for mod_name in _ALL_DOMAIN_SUBMODULES:
        mod = sys.modules[mod_name]
        assert mod.__dict__.get(attr_name) is sentinel, (
            f"{mod_name} did not receive forwarded write for {attr_name}"
        )


_HANDLERS_PACKAGE = "code_indexer.server.mcp.handlers"


@contextmanager
def _swap_app_module(sentinel: Any) -> Generator[None, None, None]:
    """Temporarily replace app_module, restoring original on exit.

    A write of ``app_module`` on the package or on ``_legacy`` is forwarded
    into ``_utils``, ``_legacy`` and every domain submodule that imported the
    name (e.g. ``git_write``).  The ``app_module`` binding of the package and
    of EVERY loaded handlers module is therefore snapshotted and restored
    exactly -- through ``__dict__``, so the restore itself is not forwarded.
    A binding left holding the MagicMock would be read by later tests'
    handlers (and saved as "original" by later patch() calls).
    """
    _ensure_modules_loaded()
    missing = object()
    modules = [
        mod
        for name, mod in list(sys.modules.items())
        if mod is not None
        and (name == _HANDLERS_PACKAGE or name.startswith(_HANDLERS_PACKAGE + "."))
    ]
    originals = [(mod, mod.__dict__.get("app_module", missing)) for mod in modules]
    try:
        yield
    finally:
        for mod, original in originals:
            if original is missing:
                mod.__dict__.pop("app_module", None)
            else:
                mod.__dict__["app_module"] = original


# ---------------------------------------------------------------------------
# _LegacyForwardingModule tests (L1 + L2 fixes)
# ---------------------------------------------------------------------------


class TestLegacyForwardingSubmoduleList:
    """Verify _LegacyForwardingModule forwards to all 11 domain submodules."""

    def test_all_domain_submodules_receive_forwarded_writes(self) -> None:
        """Setting an attribute on _legacy propagates to every submodule."""
        import code_indexer.server.mcp.handlers._legacy as legacy

        attr = "_test_legacy_fwd_all"
        with _sentinel_across_submodules(attr) as sentinel:
            setattr(legacy, attr, sentinel)
            _assert_forwarded_to_all(attr, sentinel)
            legacy.__dict__.pop(attr, None)

    def test_cicd_submodule_receives_forwarded_writes(self) -> None:
        """Regression for L1: cicd must be in the forwarding list."""
        import code_indexer.server.mcp.handlers._legacy as legacy
        import code_indexer.server.mcp.handlers.cicd as cicd

        attr = "_test_legacy_fwd_cicd"
        cicd.__dict__[attr] = None
        try:
            setattr(legacy, attr, "cicd_sentinel")
            assert cicd.__dict__[attr] == "cicd_sentinel"
        finally:
            cicd.__dict__.pop(attr, None)
            legacy.__dict__.pop(attr, None)


class TestLegacyForwardingAppModule:
    """Verify _LegacyForwardingModule forwards app_module to _utils."""

    def test_app_module_write_propagates_to_utils(self) -> None:
        """Regression for L2: _legacy.app_module write updates _utils."""
        import code_indexer.server.mcp.handlers._legacy as legacy
        import code_indexer.server.mcp.handlers._utils as utils

        sentinel = MagicMock(name="mock_app_module_legacy")
        with _swap_app_module(sentinel):
            setattr(legacy, "app_module", sentinel)
            assert utils.app_module is sentinel, (
                "_utils.app_module not updated by _legacy.app_module write"
            )


# ---------------------------------------------------------------------------
# _ForwardingModule in __init__.py (regression guard)
# ---------------------------------------------------------------------------


class TestInitForwardingSubmoduleList:
    """Verify __init__.py _ForwardingModule forwards to all submodules."""

    def test_all_domain_submodules_receive_forwarded_writes(self) -> None:
        """Setting an attribute on handlers package propagates to all."""
        import code_indexer.server.mcp.handlers as handlers

        attr = "_test_init_fwd_all"
        with _sentinel_across_submodules(attr) as sentinel:
            setattr(handlers, attr, sentinel)
            _assert_forwarded_to_all(attr, sentinel)
            handlers.__dict__.pop(attr, None)


class TestInitForwardingAppModule:
    """Verify __init__.py _ForwardingModule forwards app_module to _utils."""

    def test_app_module_write_propagates_to_utils(self) -> None:
        """handlers.app_module write updates _utils.app_module."""
        import code_indexer.server.mcp.handlers as handlers
        import code_indexer.server.mcp.handlers._utils as utils

        sentinel = MagicMock(name="mock_app_module_init")
        with _swap_app_module(sentinel):
            setattr(handlers, "app_module", sentinel)
            assert utils.app_module is sentinel, (
                "_utils.app_module not updated by handlers.app_module write"
            )
