"""Selecting the ripgrep backend, and refusing to guess when delegation breaks.

The third seam on this pattern, after git and content. Its no-fallback rule has
a different reason from the content seam's: an image built for delegation may
not ship the ripgrep binary at all, so a silent fallback does not quietly do the
wrong thing -- it fails at exec time, somewhere far from the configuration that
caused it, and reads as a broken image rather than a misconfigured one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional, Sequence

import pytest

from code_indexer.server.startup.ripgrep_backend_wiring import (
    VALID_RIPGREP_BACKENDS,
    build_configured_ripgrep_backend,
    build_ripgrep_backend,
)
from code_indexer.server.storage.shared.ripgrep_backend import LocalRipgrepBackend


class _StubDelegatedBackend:
    """Satisfies the RipgrepBackend protocol without running anything.

    The real signature, not a convenient approximation: a stub that merely
    looked backend-shaped would let the wiring accept something the protocol
    would reject, and the type checker is the only thing that notices.
    """

    @property
    def is_local(self) -> bool:
        return False

    async def run(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> Any:
        raise AssertionError("not executed in these tests")

    async def supports_pcre2(self) -> bool:
        raise AssertionError("not asked in these tests")


# ── the default ────────────────────────────────────────────────────────────


def test_an_absent_config_field_selects_local() -> None:
    """A partial/bootstrap config must not raise AttributeError."""
    assert isinstance(build_ripgrep_backend(SimpleNamespace()), LocalRipgrepBackend)


def test_an_explicit_local_selects_local() -> None:
    backend = build_ripgrep_backend(SimpleNamespace(ripgrep_backend="local"))
    assert isinstance(backend, LocalRipgrepBackend)


@pytest.mark.parametrize("falsy", ["", None])
def test_a_falsy_value_falls_back_to_local(falsy: Optional[str]) -> None:
    """A config round-tripped through JSON can arrive as "" rather than unset."""
    backend = build_ripgrep_backend(SimpleNamespace(ripgrep_backend=falsy))
    assert isinstance(backend, LocalRipgrepBackend)


def test_the_worker_count_reaches_the_local_backend() -> None:
    """Dropped on the floor, regex search silently loses its concurrency."""
    backend = build_ripgrep_backend(SimpleNamespace(), max_workers=7)
    assert (
        getattr(backend, "_max_workers", None) == 7
        or getattr(backend, "max_workers", None) == 7
    ), "max_workers never reached LocalRipgrepBackend"


# ── delegation ─────────────────────────────────────────────────────────────


def test_delegated_returns_the_injected_backend() -> None:
    stub = _StubDelegatedBackend()
    backend = build_ripgrep_backend(
        SimpleNamespace(ripgrep_backend="delegated"), delegated_backend=stub
    )
    assert backend is stub
    assert backend.is_local is False


def test_delegated_without_a_backend_raises() -> None:
    with pytest.raises(RuntimeError, match="no delegated backend"):
        build_ripgrep_backend(SimpleNamespace(ripgrep_backend="delegated"))


def test_the_refusal_names_the_remedy_an_operator_can_act_on() -> None:
    """Asserts the WIRING's message, not merely that something raised.

    RipgrepBackendFactory refuses a null delegated backend too, with a similar
    message, so deleting this guard still produces a RuntimeError. The
    distinguishing half is the remedy -- only the wiring names the entry-point
    group and the env var. Without asserting that, removing the guard is
    invisible.
    """
    with pytest.raises(RuntimeError) as excinfo:
        build_ripgrep_backend(SimpleNamespace(ripgrep_backend="delegated"))
    message = str(excinfo.value)
    assert "code_indexer.ripgrep_backend entry point" in message
    assert "CIDX_RIPGREP_BACKEND" in message


# ── typos ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["Local", "LOCAL", "deleGated", "remote", "rg"])
def test_an_unrecognised_value_raises_instead_of_defaulting(value: str) -> None:
    with pytest.raises(RuntimeError, match="Unknown ripgrep_backend"):
        build_ripgrep_backend(SimpleNamespace(ripgrep_backend=value))


def test_the_valid_set_is_exactly_local_and_delegated() -> None:
    assert VALID_RIPGREP_BACKENDS == ("local", "delegated")


# ── the configured entry point ─────────────────────────────────────────────


def test_the_local_default_never_resolves_the_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken plugin in the image must not break servers that never opted in."""
    from code_indexer.server.services import ripgrep_backend_plugin

    def explode() -> Any:
        raise AssertionError("the plugin was resolved on the local path")

    monkeypatch.setattr(
        ripgrep_backend_plugin, "resolve_delegated_ripgrep_backend", explode
    )
    backend = build_configured_ripgrep_backend(SimpleNamespace(ripgrep_backend="local"))
    assert isinstance(backend, LocalRipgrepBackend)


def test_delegated_resolves_through_the_plugin_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.services import ripgrep_backend_plugin

    stub = _StubDelegatedBackend()
    monkeypatch.setattr(
        ripgrep_backend_plugin, "resolve_delegated_ripgrep_backend", lambda: stub
    )
    assert (
        build_configured_ripgrep_backend(SimpleNamespace(ripgrep_backend="delegated"))
        is stub
    )


def test_delegated_with_no_plugin_installed_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam returning None means 'nothing installed', not 'use local'."""
    from code_indexer.server.services import ripgrep_backend_plugin

    monkeypatch.setattr(
        ripgrep_backend_plugin, "resolve_delegated_ripgrep_backend", lambda: None
    )
    with pytest.raises(RuntimeError, match="no delegated backend"):
        build_configured_ripgrep_backend(SimpleNamespace(ripgrep_backend="delegated"))


# ── the config field, and its bootstrap classification ─────────────────────


def test_server_config_declares_ripgrep_backend_defaulting_to_local() -> None:
    import dataclasses

    from code_indexer.server.utils.config_manager import ServerConfig

    fields = {f.name: f for f in dataclasses.fields(ServerConfig)}
    assert "ripgrep_backend" in fields, "the seam cannot be selected at all"
    assert fields["ripgrep_backend"].default == "local"


def test_ripgrep_backend_is_a_bootstrap_key() -> None:
    """It is read before the DB exists, like clone/git/scip/content.

    Left unclassified, config.json is stripped on first boot and the setting
    reverts to local -- regex search would silently go back to scanning a mount
    the pod may not have.
    """
    from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

    assert "ripgrep_backend" in BOOTSTRAP_KEYS


# ── this seam is its own ───────────────────────────────────────────────────


def test_the_plugin_seam_uses_its_own_names() -> None:
    """Sharing the resolver class must not mean sharing a group or an env var."""
    from code_indexer.server.services import ripgrep_backend_plugin as rg

    assert rg.ENV_VAR == "CIDX_RIPGREP_BACKEND"
    assert rg.ENTRY_POINT_GROUP == "code_indexer.ripgrep_backend"


def test_the_three_seams_do_not_share_a_group_or_env_var() -> None:
    from code_indexer.server.services import content_backend_plugin as content
    from code_indexer.server.services import git_backend_plugin as git
    from code_indexer.server.services import ripgrep_backend_plugin as rg

    groups = {git.ENTRY_POINT_GROUP, content.ENTRY_POINT_GROUP, rg.ENTRY_POINT_GROUP}
    envs = {git.ENV_VAR, content.ENV_VAR, rg.ENV_VAR}
    assert len(groups) == 3, f"two seams share an entry-point group: {groups}"
    assert len(envs) == 3, f"two seams share an env var: {envs}"
