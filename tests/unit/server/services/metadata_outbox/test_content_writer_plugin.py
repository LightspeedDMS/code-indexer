"""The content-writer plugin seam (workspace-mcp's file-write client).

Resolved like the other backend plugins; not yet wired into the outbox sink
(the write path has not published). These tests pin the seam's contract so
wiring it later is a one-line change.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from code_indexer.server.services import content_writer_plugin
from code_indexer.server.services.metadata_outbox.sinks import ContentWriterSink


class _Writer:
    def __init__(self) -> None:
        self.puts: list = []

    def put(self, path: str, data: bytes) -> str:
        self.puts.append((path, data))
        return "sha"

    def delete(self, path: str) -> None:
        self.puts.append((path, None))


def make_content_writer() -> _Writer:
    return _Writer()


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    content_writer_plugin.reset_cache()
    yield
    content_writer_plugin.reset_cache()


def test_nothing_configured_resolves_to_none(monkeypatch):
    monkeypatch.delenv(content_writer_plugin.ENV_VAR, raising=False)
    monkeypatch.setattr(
        content_writer_plugin._resolver,
        "entry_point_group",
        "code_indexer.content_writer.test-none",
    )
    assert content_writer_plugin.resolve_content_writer() is None


def test_env_var_factory_is_resolved_and_adapts_to_the_sink(monkeypatch):
    monkeypatch.setenv(
        content_writer_plugin.ENV_VAR,
        f"{__name__}:make_content_writer",
    )
    writer = content_writer_plugin.resolve_content_writer()
    assert isinstance(writer, _Writer)

    ContentWriterSink(writer).put("/g/aliases/a.json", b"{}")
    assert writer.puts == [("/g/aliases/a.json", b"{}")]


def test_unloadable_factory_fails_loud(monkeypatch):
    monkeypatch.setenv(content_writer_plugin.ENV_VAR, "no.such.module:factory")
    with pytest.raises(content_writer_plugin.ContentWriterPluginError):
        content_writer_plugin.resolve_content_writer()
