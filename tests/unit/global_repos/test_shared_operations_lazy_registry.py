"""
Unit tests for GlobalRepoOperations lazy registry resolution.

Verifies that constructing GlobalRepoOperations in postgres mode does NOT
emit a WARNING when backend_registry is not yet set on app.state at
construction time (which happens during startup before the registry is
wired up). The lazy property resolves it at request time instead.
"""

import logging
import importlib
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

# Pre-import code_indexer.server so patch("code_indexer.server.app", ...) works
# even when running in the full test suite where the package is already loaded.
# ImportError is expected and intentionally suppressed here: these tests must also
# pass in CLI-only environments where code_indexer.server is not installed.
# The patch.dict("sys.modules", ...) context managers below handle both paths.
try:
    import code_indexer.server  # noqa: F401
except ImportError:
    pass  # CLI-only environment: server package absent, patch.dict handles isolation


def _make_app_state(storage_mode: str, backend_registry=None):
    """Create a fake app.state with the given storage_mode and backend_registry."""
    return SimpleNamespace(
        storage_mode=storage_mode,
        backend_registry=backend_registry,
    )


def _make_app(state):
    """Create a fake app object whose .state is the given state."""
    return SimpleNamespace(state=state)


# get_server_global_registry is lazily imported inside GlobalRepoOperations
# methods, so we patch it at its definition location in registry_factory.
REGISTRY_FACTORY_PATCH = (
    "code_indexer.server.utils.registry_factory.get_server_global_registry"
)


def test_no_warning_when_backend_registry_not_yet_set(tmp_path, caplog):
    """
    GlobalRepoOperations construction MUST NOT emit a WARNING when
    storage_mode=postgres but backend_registry is not yet set (None).

    This scenario occurs during server startup in postgres mode:
    GlobalReposLifecycleManager is constructed before app.state.backend_registry
    is populated. With the lazy property fix the warning is deferred until
    (and unless) actual access happens without a backend.
    """
    golden_repos_dir = str(tmp_path / "golden-repos")

    fake_state = _make_app_state(storage_mode="postgres", backend_registry=None)
    fake_app = _make_app(fake_state)
    fake_app_module = SimpleNamespace(app=fake_app)
    mock_registry = MagicMock()

    import code_indexer.global_repos.shared_operations as mod

    importlib.reload(mod)

    with patch.dict("sys.modules", {"code_indexer.server.app": fake_app_module}):
        with patch("code_indexer.server.app", fake_app_module, create=True):
            with patch(REGISTRY_FACTORY_PATCH, return_value=mock_registry):
                with caplog.at_level(
                    logging.WARNING,
                    logger="code_indexer.global_repos.shared_operations",
                ):
                    ops = mod.GlobalRepoOperations(golden_repos_dir=golden_repos_dir)

    warning_messages = [
        r.message for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert not any("backend_registry not set" in m for m in warning_messages), (
        f"Unexpected WARNING emitted during construction: {warning_messages}"
    )
    assert ops is not None


def test_lazy_registry_resolves_postgres_backend_at_access_time(tmp_path):
    """
    Verifies two things about lazy resolution:
    1. get_server_global_registry is NOT called at construction time when
       backend_registry is None (factory call count == 0 after __init__).
    2. When .registry is accessed after backend_registry becomes available,
       get_server_global_registry IS called with backend=<pg_global_repos_backend>,
       proving the lazy property picks up the postgres backend at request time.
    """
    golden_repos_dir = str(tmp_path / "golden-repos")

    fake_state = _make_app_state(storage_mode="postgres", backend_registry=None)
    fake_app = _make_app(fake_state)
    fake_app_module = SimpleNamespace(app=fake_app)

    import code_indexer.global_repos.shared_operations as mod

    importlib.reload(mod)

    mock_factory = MagicMock(name="get_server_global_registry")
    mock_pg_registry = MagicMock(name="pg_registry")
    mock_factory.return_value = mock_pg_registry

    with patch.dict("sys.modules", {"code_indexer.server.app": fake_app_module}):
        with patch("code_indexer.server.app", fake_app_module, create=True):
            with patch(REGISTRY_FACTORY_PATCH, mock_factory):
                ops = mod.GlobalRepoOperations(golden_repos_dir=golden_repos_dir)

                # Assert: factory must NOT have been called at construction time
                assert mock_factory.call_count == 0, (
                    f"get_server_global_registry was called {mock_factory.call_count} "
                    "time(s) at construction — must be deferred until .registry access"
                )

                # Simulate backend_registry becoming available at request time
                mock_backend = MagicMock()
                mock_backend.global_repos = MagicMock(name="pg_global_repos_backend")
                fake_state.backend_registry = mock_backend

                # Access .registry — this triggers lazy resolution
                registry = ops.registry

    # Assert: factory called exactly once, with the postgres backend
    mock_factory.assert_called_once()
    _, call_kwargs = mock_factory.call_args
    assert call_kwargs.get("backend") is mock_backend.global_repos, (
        f"Expected backend=mock_backend.global_repos, "
        f"got: {call_kwargs.get('backend')!r}"
    )
    assert registry is mock_pg_registry


def test_two_phase_resolution(tmp_path):
    """
    Exercises two-phase lazy resolution:

    Phase 1: First .registry access with backend_registry=None
      - factory is called (SQLite fallback), result is NOT cached because
        postgres mode lacks a backend yet.

    Phase 2: backend_registry becomes available; second .registry access
      - factory is called again with the postgres backend, result IS cached.

    Phase 3: Third .registry access
      - factory is NOT called again (cache hit).
    """
    golden_repos_dir = str(tmp_path / "golden-repos")

    fake_state = _make_app_state(storage_mode="postgres", backend_registry=None)
    fake_app = _make_app(fake_state)
    fake_app_module = SimpleNamespace(app=fake_app)

    import code_indexer.global_repos.shared_operations as mod

    importlib.reload(mod)

    mock_factory = MagicMock(name="get_server_global_registry")
    mock_sqlite_fallback = MagicMock(name="sqlite_fallback")
    mock_pg_registry = MagicMock(name="pg_registry")
    mock_factory.side_effect = [mock_sqlite_fallback, mock_pg_registry]

    with patch.dict("sys.modules", {"code_indexer.server.app": fake_app_module}):
        with patch("code_indexer.server.app", fake_app_module, create=True):
            with patch(REGISTRY_FACTORY_PATCH, mock_factory):
                ops = mod.GlobalRepoOperations(golden_repos_dir=golden_repos_dir)

                # Phase 1: no backend yet — factory called, result NOT cached
                result1 = ops.registry
                assert result1 is mock_sqlite_fallback
                assert mock_factory.call_count == 1
                assert ops._registry is None, (
                    "Result must NOT be cached when postgres backend is unavailable"
                )

                # Simulate backend becoming available
                mock_backend = MagicMock()
                mock_backend.global_repos = MagicMock(name="pg_global_repos_backend")
                fake_state.backend_registry = mock_backend

                # Phase 2: backend available — factory called with postgres backend, result IS cached
                result2 = ops.registry
                assert result2 is mock_pg_registry
                assert mock_factory.call_count == 2
                _, call_kwargs = mock_factory.call_args
                assert call_kwargs.get("backend") is mock_backend.global_repos
                assert ops._registry is mock_pg_registry, (
                    "Result must be cached after successful postgres resolution"
                )

                # Phase 3: cache hit — factory NOT called again
                result3 = ops.registry
                assert result3 is mock_pg_registry
                assert mock_factory.call_count == 2, (
                    "Factory must not be called again after cache is populated"
                )


def test_registry_caches_on_first_access_with_backend(tmp_path):
    """
    When backend_registry is already available at first .registry access,
    the result is cached immediately and subsequent accesses return the same
    object without calling the factory again.
    """
    golden_repos_dir = str(tmp_path / "golden-repos")

    mock_backend = MagicMock()
    mock_backend.global_repos = MagicMock(name="pg_global_repos_backend")
    fake_state = _make_app_state(storage_mode="postgres", backend_registry=mock_backend)
    fake_app = _make_app(fake_state)
    fake_app_module = SimpleNamespace(app=fake_app)

    import code_indexer.global_repos.shared_operations as mod

    importlib.reload(mod)

    mock_factory = MagicMock(name="get_server_global_registry")
    mock_pg_registry = MagicMock(name="pg_registry")
    mock_factory.return_value = mock_pg_registry

    with patch.dict("sys.modules", {"code_indexer.server.app": fake_app_module}):
        with patch("code_indexer.server.app", fake_app_module, create=True):
            with patch(REGISTRY_FACTORY_PATCH, mock_factory):
                ops = mod.GlobalRepoOperations(golden_repos_dir=golden_repos_dir)

                # First access — factory called once, result cached
                result1 = ops.registry
                assert result1 is mock_pg_registry
                assert mock_factory.call_count == 1
                assert ops._registry is mock_pg_registry

                # Second access — cache hit, factory NOT called again
                result2 = ops.registry
                assert result2 is mock_pg_registry
                assert mock_factory.call_count == 1, (
                    "Factory must not be called on second access when result is cached"
                )


def test_registry_falls_back_to_sqlite_when_no_app_module(tmp_path, caplog):
    """
    When the server app module is not available (CLI mode / ImportError),
    construction must not raise, ops.registry must return the SQLite registry,
    and no WARNING about backend_registry must be emitted.
    """
    golden_repos_dir = str(tmp_path / "golden-repos")

    mock_sqlite_registry = MagicMock(name="sqlite_registry")

    import code_indexer.global_repos.shared_operations as mod

    importlib.reload(mod)

    # Setting a sys.modules entry to None causes "import X" to raise ImportError.
    with patch.dict("sys.modules", {"code_indexer.server.app": None}):
        with patch(REGISTRY_FACTORY_PATCH, return_value=mock_sqlite_registry):
            with caplog.at_level(
                logging.WARNING,
                logger="code_indexer.global_repos.shared_operations",
            ):
                ops = mod.GlobalRepoOperations(golden_repos_dir=golden_repos_dir)

            # Assert: ops.registry returns the SQLite registry (inside patch scope)
            registry = ops.registry
            assert registry is mock_sqlite_registry, (
                f"Expected SQLite registry in CLI mode, got: {registry!r}"
            )

    # Assert: no backend_registry WARNING was emitted in CLI mode
    warning_messages = [
        r.message for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert not any("backend_registry not set" in m for m in warning_messages), (
        f"Unexpected WARNING in CLI mode: {warning_messages}"
    )
