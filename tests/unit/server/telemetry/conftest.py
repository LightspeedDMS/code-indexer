"""Shared fixtures for the telemetry tests."""

from __future__ import annotations

from typing import Iterator

import pytest

from tests.unit.server.telemetry.otlp_sink import OtlpHttpSink


@pytest.fixture(scope="session")
def otlp_sink() -> Iterator[OtlpHttpSink]:
    """A listening OTLP/HTTP collector on an ephemeral loopback port: every
    telemetry test that exports points its endpoint here."""
    sink = OtlpHttpSink().start()
    try:
        yield sink
    finally:
        sink.stop()
