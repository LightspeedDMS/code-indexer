"""Selecting the content backend, and refusing to guess when delegation breaks.

The no-fallback rule matters more for this seam than for any other. A pod
configured for delegation may not mount the repositories at all, and
``LocalContentBackend`` walking a directory that is not there does not raise --
``os.walk`` over a missing tree simply yields nothing. So a silent fallback
would report an EMPTY repository, index zero files, and look like a clean run.
Every test below exists to keep that failure impossible.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Optional, Sequence

import pytest

from code_indexer.server.startup.content_backend_wiring import (
    VALID_CONTENT_BACKENDS,
    build_configured_content_backend,
    build_content_backend,
)
from code_indexer.server.storage.shared.content_backend import LocalContentBackend


class _StubDelegatedBackend:
    """Satisfies the ContentBackend protocol without touching a filesystem."""

    is_local = False

    def manifest(
        self,
        repo_path: Any,
        *,
        cursor: Optional[str] = None,
        page_size: int = 1000,
    ) -> Any:
        raise AssertionError("not called in these tests")

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[Any]:
        raise AssertionError("not called in these tests")


# ── the default ────────────────────────────────────────────────────────────


def test_an_absent_config_field_selects_local() -> None:
    """A partial/bootstrap config must not raise AttributeError."""
    backend = build_content_backend(SimpleNamespace())
    assert isinstance(backend, LocalContentBackend)


def test_an_explicit_local_selects_local() -> None:
    backend = build_content_backend(SimpleNamespace(content_backend="local"))
    assert isinstance(backend, LocalContentBackend)


def test_an_empty_string_falls_back_to_local_not_an_error() -> None:
    """A config round-tripped through JSON can arrive as "" rather than unset."""
    backend = build_content_backend(SimpleNamespace(content_backend=""))
    assert isinstance(backend, LocalContentBackend)


def test_none_falls_back_to_local() -> None:
    backend = build_content_backend(SimpleNamespace(content_backend=None))
    assert isinstance(backend, LocalContentBackend)


# ── delegation ─────────────────────────────────────────────────────────────


def test_delegated_returns_the_injected_backend() -> None:
    stub = _StubDelegatedBackend()
    backend = build_content_backend(
        SimpleNamespace(content_backend="delegated"), delegated_backend=stub
    )
    assert backend is stub
    assert backend.is_local is False


def test_delegated_without_a_backend_raises_rather_than_reading_the_disk() -> None:
    """The whole point. Falling back here would index an empty repository."""
    with pytest.raises(RuntimeError, match="no delegated backend"):
        build_content_backend(SimpleNamespace(content_backend="delegated"))


def test_the_refusal_names_the_remedy_an_operator_can_act_on() -> None:
    """Asserts the WIRING's message, not merely that something raised.

    ContentBackendFactory refuses a null delegated backend as well, so deleting
    this guard entirely still produces a RuntimeError and still mentions the
    empty-repository rationale -- the two messages are nearly identical. The
    distinguishing half is the remedy: only the wiring names the entry-point
    group and the env var, which is what an operator needs to actually fix it.
    Without asserting that, removing this guard is invisible.
    """
    with pytest.raises(RuntimeError) as excinfo:
        build_content_backend(SimpleNamespace(content_backend="delegated"))
    message = str(excinfo.value)
    assert "code_indexer.content_backend entry point" in message, (
        "the refusal does not name the entry-point group to register"
    )
    assert "CIDX_CONTENT_BACKEND" in message, (
        "the refusal does not name the env var alternative"
    )
    assert "may not mount the repositories" in message


# ── typos ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["Local", "LOCAL", "deleGated", "remote", "mcp"])
def test_an_unrecognised_value_raises_instead_of_defaulting(value: str) -> None:
    """A typo'd config must not degrade into a surprising default.

    Case matters: 'Local' silently treated as local would mean a config that
    reads as deliberate is doing something nobody chose.
    """
    with pytest.raises(RuntimeError, match="Unknown content_backend"):
        build_content_backend(SimpleNamespace(content_backend=value))


def test_the_valid_set_is_exactly_local_and_delegated() -> None:
    assert VALID_CONTENT_BACKENDS == ("local", "delegated")


# ── the configured entry point ─────────────────────────────────────────────


def test_the_local_default_never_resolves_the_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing deployments must not pay for, or fail on, a plugin they never set.

    A broken plugin installed in the image would otherwise break every server
    that had not opted into delegation at all.
    """
    from code_indexer.server.services import content_backend_plugin

    def explode() -> Any:
        raise AssertionError("the plugin was resolved on the local path")

    monkeypatch.setattr(
        content_backend_plugin, "resolve_delegated_content_backend", explode
    )
    backend = build_configured_content_backend(SimpleNamespace(content_backend="local"))
    assert isinstance(backend, LocalContentBackend)


def test_delegated_resolves_through_the_plugin_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.services import content_backend_plugin

    stub = _StubDelegatedBackend()
    monkeypatch.setattr(
        content_backend_plugin, "resolve_delegated_content_backend", lambda: stub
    )
    backend = build_configured_content_backend(
        SimpleNamespace(content_backend="delegated")
    )
    assert backend is stub


def test_delegated_with_no_plugin_installed_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam returning None means 'nothing installed', not 'use local'."""
    from code_indexer.server.services import content_backend_plugin

    monkeypatch.setattr(
        content_backend_plugin, "resolve_delegated_content_backend", lambda: None
    )
    with pytest.raises(RuntimeError, match="no delegated backend"):
        build_configured_content_backend(SimpleNamespace(content_backend="delegated"))


# ── the config field itself ────────────────────────────────────────────────


def test_server_config_declares_content_backend_defaulting_to_local() -> None:
    """Without the field an operator has no way to select delegation at all."""
    from code_indexer.server.utils.config_manager import ServerConfig

    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(ServerConfig)}
    assert "content_backend" in fields, (
        "ServerConfig has no content_backend field, so the seam cannot be selected"
    )
    assert fields["content_backend"].default == "local"


def test_the_local_backend_really_reads_this_filesystem(tmp_path: Path) -> None:
    """Anchors the default to observable behaviour, not just a type name.

    If LocalContentBackend were ever swapped for something inert, every test
    above would still pass on isinstance alone.
    """
    (tmp_path / "a.py").write_bytes(b"x = 1\n")
    backend = build_content_backend(SimpleNamespace())
    page = backend.manifest(tmp_path)
    assert [entry.path for entry in page.entries] == ["a.py"]
