"""Source-text wiring guards for MemoryGovernor (Story #1213 Story 1).

Mirrors test_lifespan_coalescer_registry_wiring.py.
These tests FAIL before the wiring is added and PASS after.
"""

from pathlib import Path

_REPO_ROOT_PARENT_INDEX = 4
_REPO_ROOT = Path(__file__).resolve().parents[_REPO_ROOT_PARENT_INDEX]

_SERVICE_INIT_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "service_init.py"
)
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)
_GOVERNOR_MODULE_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "services" / "memory_governor.py"
)


class TestServiceInitWiring:
    """Source-text guards: governor built in service_init.py; cleared in lifespan.py."""

    def test_service_init_references_governor(self):
        source = _SERVICE_INIT_PATH.read_text()
        assert "memory_governor" in source.lower(), (
            "service_init.py must build/wire the MemoryGovernor after initialize_caches"
        )

    def test_lifespan_clears_governor_on_shutdown(self):
        source = _LIFESPAN_PATH.read_text()
        assert "clear_memory_governor" in source, (
            "lifespan.py must call clear_memory_governor() on shutdown (after yield)"
        )

    def test_getter_none_source_text_guard(self):
        """memory_governor.py must define get_memory_governor() that can return None.

        This is a source-text guard: verifies the module implements the None-on-CLI
        contract (get_memory_governor returns Optional[MemoryGovernor], defaults None).
        """
        source = _GOVERNOR_MODULE_PATH.read_text()
        assert "get_memory_governor" in source, (
            "memory_governor.py must define get_memory_governor()"
        )
        assert "None" in source, (
            "memory_governor.py get_memory_governor must be able to return None "
            "(CLI/pre-init case — the None-on-CLI contract)"
        )

    def test_service_init_starts_sampler(self):
        """service_init.py must call _memory_governor.start() after set_memory_governor().

        Source-text guard: the exact call must be present so the sampler thread
        actually runs in server mode (anti-orphan — built but never run violates §3.2).
        """
        source = _SERVICE_INIT_PATH.read_text()
        assert "_memory_governor.start()" in source, (
            "service_init.py must call _memory_governor.start() after set_memory_governor() "
            "so the sampler thread runs in server mode (design §3.2)"
        )

    def test_lifespan_stops_sampler_before_clear(self):
        """lifespan.py must call governor.stop() BEFORE clear_memory_governor() on shutdown.

        Source-text guard using position comparison: stop() must appear before
        clear_memory_governor() so the sampler thread is cleanly joined.
        """
        source = _LIFESPAN_PATH.read_text()
        stop_pos = source.find(".stop(")
        clear_pos = source.find("clear_memory_governor")
        assert stop_pos != -1, (
            "lifespan.py must contain a governor .stop() call on shutdown"
        )
        assert clear_pos != -1, (
            "lifespan.py must contain clear_memory_governor() call on shutdown"
        )
        assert stop_pos < clear_pos, (
            f"governor.stop() (pos {stop_pos}) must appear before "
            f"clear_memory_governor() (pos {clear_pos}) in lifespan.py"
        )


# Generous timeout for CI: sampler thread uses a short interval in tests
_STOP_TIMEOUT_SECONDS = 2.0


class TestSamplerWiringFunctional:
    """Functional tests validating the start/stop lifecycle pattern required by wiring.

    These tests exercise MemoryGovernor directly to document and verify the exact
    contract that service_init.py (start) and lifespan.py (stop+clear) must follow.
    The source-text guards in TestServiceInitWiring catch the actual production calls;
    these tests catch lifecycle correctness bugs (e.g. stop() not joining the thread).
    """

    def test_governor_sampler_starts_in_service_mode(self):
        """After build+start (as service_init does), is_running() must be True."""
        from tests.unit.server.services.test_memory_governor_fixtures import (
            FakeMemoryReaders,
        )
        from code_indexer.server.services.memory_governor import (
            MemoryGovernor,
            clear_memory_governor,
            set_memory_governor,
        )

        readers = FakeMemoryReaders()
        gov = MemoryGovernor(readers=readers, enabled=True, start_sampler=False)
        set_memory_governor(gov)
        gov.start()
        try:
            assert gov.is_running(), (
                "Sampler thread must be alive after start() — "
                "validates the service_init.py server-mode wiring pattern"
            )
        finally:
            gov.stop(timeout=_STOP_TIMEOUT_SECONDS)
            clear_memory_governor()

    def test_governor_sampler_stops_before_clear(self):
        """After stop()+clear() (as lifespan does), thread dead and singleton None."""
        from tests.unit.server.services.test_memory_governor_fixtures import (
            FakeMemoryReaders,
        )
        from code_indexer.server.services.memory_governor import (
            MemoryGovernor,
            clear_memory_governor,
            get_memory_governor,
            set_memory_governor,
        )

        readers = FakeMemoryReaders()
        gov = MemoryGovernor(readers=readers, enabled=True, start_sampler=False)
        set_memory_governor(gov)
        gov.start()
        try:
            assert gov.is_running(), (
                "Precondition: sampler must be running before stop()"
            )
            gov.stop(timeout=_STOP_TIMEOUT_SECONDS)
            assert not gov.is_running(), (
                "Sampler thread must be dead after stop() — "
                "validates the lifespan.py shutdown ordering (stop before clear)"
            )
            clear_memory_governor()
            assert get_memory_governor() is None, (
                "get_memory_governor() must return None after clear_memory_governor()"
            )
        finally:
            # Safety net: ensure cleanup even if an assertion fails mid-test
            if gov.is_running():
                gov.stop(timeout=_STOP_TIMEOUT_SECONDS)
            if get_memory_governor() is not None:
                clear_memory_governor()
