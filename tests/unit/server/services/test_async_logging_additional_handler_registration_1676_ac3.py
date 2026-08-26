"""
Story #1676 AC3 Requirement 4: async_logging must expose an explicit
registration API (register_additional_listener_handler /
unregister_additional_listener_handler) so a handler can be added to the
ALREADY-RUNNING QueueListener's real-handler set AFTER telemetry
initializes (which happens ~2600 lines later in lifespan.py than
install_queue_logging's own call site) -- the listener's handler tuple is
fixed at construction time otherwise, and mutating whatever tuple was
captured then is the only way to add a handler post-hoc.

Idempotency: registering the SAME handler object twice must not create a
duplicate entry (verified via the API's own return value / listener state,
not merely "no exception raised") -- this is exactly the guarantee the
story's required test list calls for ("no duplicate on repeated
TelemetryManager construction/reset").
"""

from __future__ import annotations

import logging

from code_indexer.server.services.async_logging import (
    install_queue_logging,
    register_additional_listener_handler,
    shutdown_queue_logging,
    unregister_additional_listener_handler,
)


class _NullHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        pass


def _install_listener():
    base_handler = _NullHandler()
    listener = install_queue_logging([base_handler])
    return listener, base_handler


class TestRegisterAdditionalListenerHandler:
    def test_registers_new_handler_and_returns_true(self) -> None:
        listener, _base = _install_listener()
        try:
            extra = _NullHandler()

            result = register_additional_listener_handler(extra)

            assert result is True
            assert extra in listener.handlers
        finally:
            listener.stop()

    def test_registering_same_handler_twice_is_idempotent(self) -> None:
        listener, _base = _install_listener()
        try:
            extra = _NullHandler()

            first = register_additional_listener_handler(extra)
            second = register_additional_listener_handler(extra)

            assert first is True
            assert second is False, (
                "second registration of the SAME handler object must be a "
                "no-op (idempotent), not a duplicate append"
            )
            assert listener.handlers.count(extra) == 1
        finally:
            listener.stop()

    def test_returns_false_when_no_active_listener(self) -> None:
        # No install_queue_logging() call in this test -- simulate the
        # "telemetry disabled, listener never installed" state by stopping
        # any listener a prior test may have left active.
        shutdown_queue_logging()

        result = register_additional_listener_handler(_NullHandler())

        assert result is False


class TestUnregisterAdditionalListenerHandler:
    def test_unregisters_a_registered_handler_and_returns_true(self) -> None:
        listener, _base = _install_listener()
        try:
            extra = _NullHandler()
            assert register_additional_listener_handler(extra) is True

            result = unregister_additional_listener_handler(extra)

            assert result is True
            assert extra not in listener.handlers
        finally:
            listener.stop()

    def test_unregistering_an_unregistered_handler_returns_false(self) -> None:
        listener, _base = _install_listener()
        try:
            never_registered = _NullHandler()

            result = unregister_additional_listener_handler(never_registered)

            assert result is False
        finally:
            listener.stop()

    def test_returns_false_when_no_active_listener(self) -> None:
        shutdown_queue_logging()

        result = unregister_additional_listener_handler(_NullHandler())

        assert result is False
