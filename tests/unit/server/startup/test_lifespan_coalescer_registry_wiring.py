"""Story #1079 Phase E regression guard: lifespan wires the coalescer registry.

The coalescer registry is constructed ONCE in server lifespan startup (after
providers + runtime config are available) via ``set_coalescer_registry(
build_coalescer_registry(...))`` and cleared on shutdown via
``clear_coalescer_registry()``. Source-order matters: the set must be BEFORE the
``yield`` (server-running boundary) and the clear AFTER it.

These are source-text + source-order guards (mirroring the Bug #1044 wiring
guard). They MUST fail before the Phase E lifespan wiring and pass after.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanCoalescerRegistryWiring:
    def test_build_and_set_present_in_startup(self):
        source = _LIFESPAN_PATH.read_text()
        assert "build_coalescer_registry" in source, (
            "lifespan.py must build the coalescer registry via "
            "build_coalescer_registry(...) on startup"
        )
        assert "set_coalescer_registry" in source, (
            "lifespan.py must install the registry via set_coalescer_registry(...)"
        )

    def test_clear_present_in_shutdown(self):
        source = _LIFESPAN_PATH.read_text()
        assert "clear_coalescer_registry" in source, (
            "lifespan.py must clear the coalescer registry on shutdown via "
            "clear_coalescer_registry()"
        )

    def test_set_before_yield_and_clear_after_yield(self):
        source = _LIFESPAN_PATH.read_text()
        yield_pos = source.find("yield  # Server is now running")
        set_pos = source.find("set_coalescer_registry")
        clear_pos = source.find("clear_coalescer_registry")

        assert yield_pos != -1, "could not locate the lifespan yield boundary"
        assert set_pos != -1 and clear_pos != -1
        assert set_pos < yield_pos, (
            "set_coalescer_registry must run during STARTUP (before the yield)"
        )
        assert clear_pos > yield_pos, (
            "clear_coalescer_registry must run during SHUTDOWN (after the yield)"
        )
