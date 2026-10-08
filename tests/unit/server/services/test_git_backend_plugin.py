"""
Pluggable delegated git backend loader (EVO-64678 Phase 3 / EVO-64616).

The git sibling of ``cli_invoker_plugin``: a deployment that must run git in the
git-mcp spoke (not in the cidx image) supplies a ``GitBackend`` factory via the
``code_indexer.git_backend`` entry point or the ``CIDX_GIT_BACKEND`` env var.
With nothing configured the resolver returns None and the caller keeps
``LocalGitBackend``.

These tests pin:
  * no plugin -> factory is None (local default unchanged);
  * an env-var plugin -> its factory is resolved and ``resolve_...`` instantiates it;
  * the Python 3.9 real ``entry_points()`` signature (zero params) works; and
  * misconfiguration fails loud (``GitBackendPluginError``) rather than silently
    returning None -- which would let ``git_backend='delegated'`` fall back to the
    exact in-process git it was configured to avoid.
"""

from __future__ import annotations

import pytest

from code_indexer.server.services import git_backend_plugin
from code_indexer.server.services import backend_plugin_loader
from code_indexer.server.services.git_backend_plugin import (
    ENV_VAR,
    GitBackendPluginError,
    get_git_backend_factory,
    resolve_delegated_git_backend,
)


def _fake_entry_points_no_plugin(group=None):
    """Mirror the REAL cross-version entry_points() call shapes (3.9 takes zero
    params and returns a dict; 3.10+ accepts group= and returns an iterable) so
    the default case means "no entry point" regardless of interpreter branch."""
    if group is None:
        return {}
    return []


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    # Deterministic regardless of what is pip-installed in the dev venv: the
    # cidx_git_backend plugin may be installed locally but is not in CI.
    # The env-var/entry-point mechanism moved to backend_plugin_loader when the
    # content seam needed the same resolution. Patch it where it now LIVES:
    # patching git_backend_plugin would silently intercept nothing, and these
    # tests would pass against whatever is pip-installed in the dev venv.
    monkeypatch.setattr(
        backend_plugin_loader, "entry_points", _fake_entry_points_no_plugin
    )
    git_backend_plugin.reset_cache()
    yield
    git_backend_plugin.reset_cache()


class _StubBackend:
    """Minimal stand-in — the loader never calls GitBackend methods, only the
    zero-arg factory that produces one."""

    def __init__(self):
        self.made = True


def make_stub_backend():
    return _StubBackend()


not_callable = "nope"

_THIS = "tests.unit.server.services.test_git_backend_plugin"


class TestDefaultIsUnchanged:
    def test_no_plugin_returns_none(self):
        assert get_git_backend_factory() is None

    def test_resolve_returns_none_when_no_plugin(self):
        assert resolve_delegated_git_backend() is None


class TestEnvVarPluginResolves:
    def test_factory_loaded_from_env_var(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:make_stub_backend")
        factory = get_git_backend_factory()
        # The env-var spec re-imports this module under its full dotted path,
        # which pytest may hold under a different module name -> `is` compares two
        # function objects. Assert by name + that it produces the stub instead.
        assert factory is not None
        assert factory.__name__ == "make_stub_backend"
        assert type(factory()).__name__ == "_StubBackend"

    def test_resolve_instantiates_the_backend(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:make_stub_backend")
        backend = resolve_delegated_git_backend()
        # Double-import under a second module name makes ``is _StubBackend``
        # unreliable; compare by name.
        assert type(backend).__name__ == "_StubBackend"

    def test_factory_is_cached(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:make_stub_backend")
        first = get_git_backend_factory()
        # Second call returns the cached factory even if the env changes.
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert get_git_backend_factory() is first


class TestPython39EntryPointsRealSignature:
    """Python 3.9's REAL entry_points() takes ZERO parameters and returns a dict;
    ``group=`` arrived in 3.10. Prove the discovery path works under genuine 3.9
    semantics (the autouse fake accepts group= so cannot catch this)."""

    def test_load_from_entry_points_with_real_py39_signature(self, monkeypatch):
        class _FakeEntryPoint:
            name = "gitmcp"

            def load(self):
                return make_stub_backend

        def _fake_entry_points_py39_signature():
            return {git_backend_plugin.ENTRY_POINT_GROUP: [_FakeEntryPoint()]}

        monkeypatch.setattr(
            backend_plugin_loader, "entry_points", _fake_entry_points_py39_signature
        )
        monkeypatch.setattr(backend_plugin_loader.sys, "version_info", (3, 9, 0))
        git_backend_plugin.reset_cache()

        factory = get_git_backend_factory()

        assert factory is make_stub_backend


class TestMisconfigurationFailsLoud:
    def test_unknown_module(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "no_such_module_zzz:make")
        with pytest.raises(GitBackendPluginError, match="cannot import module"):
            get_git_backend_factory()

    def test_missing_attribute(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:does_not_exist")
        with pytest.raises(GitBackendPluginError, match="has no attribute"):
            get_git_backend_factory()

    def test_not_callable(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, f"{_THIS}:not_callable")
        with pytest.raises(GitBackendPluginError, match="not callable"):
            get_git_backend_factory()

    def test_malformed_spec(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "no.colon.here")
        with pytest.raises(GitBackendPluginError, match="package.module:factory"):
            get_git_backend_factory()
