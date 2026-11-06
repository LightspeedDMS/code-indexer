"""Unit tests for build_git_backend (EVO-64678, EVO-64616 Phase 2).

Mirrors the clone-backend wiring tests: config selects the backend, the default
is ``local`` (so existing deployments are untouched), and ``delegated`` fails
loudly rather than silently falling back to running git in cidx.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from code_indexer.server.startup.git_backend_wiring import build_git_backend
from code_indexer.server.storage.shared.git_backend import GitBackend, LocalGitBackend


def test_default_config_selects_local():
    """An unset git_backend must behave exactly as cidx does today."""
    assert isinstance(build_git_backend(SimpleNamespace()), LocalGitBackend)


def test_explicit_local_selects_local():
    cfg = SimpleNamespace(git_backend="local")
    assert isinstance(build_git_backend(cfg), LocalGitBackend)


def test_empty_value_falls_back_to_local():
    """A blank config value is treated as unset, not as an unknown backend."""
    cfg = SimpleNamespace(git_backend="")
    assert isinstance(build_git_backend(cfg), LocalGitBackend)


def test_delegated_returns_the_injected_backend():
    injected = MagicMock(spec=GitBackend)
    cfg = SimpleNamespace(git_backend="delegated")
    assert build_git_backend(cfg, delegated_backend=injected) is injected


def test_delegated_without_a_backend_raises_and_never_falls_back_to_local():
    """Delegation exists so cidx performs NO git work — no silent local fallback."""
    cfg = SimpleNamespace(git_backend="delegated")
    with pytest.raises(RuntimeError, match="no delegated backend is available"):
        build_git_backend(cfg)


def test_unknown_backend_raises_instead_of_defaulting():
    """A typo'd config must fail loudly, not degrade into a surprising default."""
    cfg = SimpleNamespace(git_backend="bogus")
    with pytest.raises(RuntimeError, match="Unknown git_backend"):
        build_git_backend(cfg)
