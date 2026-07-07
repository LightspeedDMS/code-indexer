"""Unit tests for the temporal metadata backend registry (Bug #1313 Step 4).

Mirrors the coalescer_registry.py pattern (server/services/coalescer_registry.py):
a process-level singleton holding an OPTIONAL factory. CLI/solo never set it
(so TemporalMetadataStore always uses the SQLite backend there); cluster/postgres
lifespan startup sets it once so every TemporalMetadataStore construction routes
through PostgreSQL instead.

This registry lives in the CORE layer (storage/) with ZERO server imports --
see the layering guard test in test_temporal_metadata_layering_guard.py.
"""

from pathlib import Path

import pytest


class TestTemporalMetadataBackendRegistryDefault:
    def test_get_factory_defaults_to_none(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
            get_temporal_metadata_backend_factory,
        )

        clear_temporal_metadata_backend_factory()
        assert get_temporal_metadata_backend_factory() is None


class TestTemporalMetadataBackendRegistrySetGetClear:
    def test_set_then_get_returns_the_same_factory(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
            get_temporal_metadata_backend_factory,
            set_temporal_metadata_backend_factory,
        )

        def _factory(path: Path):
            return object()

        try:
            set_temporal_metadata_backend_factory(_factory)
            assert get_temporal_metadata_backend_factory() is _factory
        finally:
            clear_temporal_metadata_backend_factory()

    def test_clear_resets_to_none(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
            get_temporal_metadata_backend_factory,
            set_temporal_metadata_backend_factory,
        )

        set_temporal_metadata_backend_factory(lambda path: object())
        clear_temporal_metadata_backend_factory()

        assert get_temporal_metadata_backend_factory() is None

    def test_factory_is_called_with_the_collection_path(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
            get_temporal_metadata_backend_factory,
            set_temporal_metadata_backend_factory,
        )

        received = []

        def _factory(path: Path):
            received.append(path)
            return "sentinel-backend"

        try:
            set_temporal_metadata_backend_factory(_factory)
            factory = get_temporal_metadata_backend_factory()
            assert factory is not None
            result = factory(Path("/some/collection"))
            assert result == "sentinel-backend"
            assert received == [Path("/some/collection")]
        finally:
            clear_temporal_metadata_backend_factory()


class TestInstallPoisonTemporalMetadataBackendFactory:
    """Bug #1313 round-2 rework (Codex Finding A).

    Postgres/cluster-mode startup must NEVER leave the registry factory
    unset on failure, because TemporalMetadataStore's facade treats "no
    factory" as "use the SQLite backend" -- silently reintroducing the
    NFS-backed SQLite-WAL Cluster-Aware-State violation this bug fixes.
    ``install_poison_temporal_metadata_backend_factory`` installs a factory
    that raises a clear, actionable RuntimeError the moment any caller
    attempts to construct a TemporalMetadataStore, instead of leaving the
    factory unset.
    """

    def test_poison_factory_raises_runtime_error_with_reason_on_use(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
            get_temporal_metadata_backend_factory,
            install_poison_temporal_metadata_backend_factory,
        )

        try:
            install_poison_temporal_metadata_backend_factory(
                "no PostgreSQL connection pool available on backend_registry"
            )
            factory = get_temporal_metadata_backend_factory()
            assert factory is not None

            with pytest.raises(RuntimeError) as excinfo:
                factory(Path("/some/collection"))

            assert "no PostgreSQL connection pool available on backend_registry" in str(
                excinfo.value
            )
            assert "Bug #1313" in str(excinfo.value)
        finally:
            clear_temporal_metadata_backend_factory()

    def test_poison_factory_is_retrievable_via_get(self):
        """The registry must never silently stay at None after this call --
        proving TemporalMetadataStore's facade will NOT fall back to the
        SQLite default."""
        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
            get_temporal_metadata_backend_factory,
            install_poison_temporal_metadata_backend_factory,
        )

        try:
            assert get_temporal_metadata_backend_factory() is None
            install_poison_temporal_metadata_backend_factory("some failure reason")
            assert get_temporal_metadata_backend_factory() is not None
        finally:
            clear_temporal_metadata_backend_factory()


class TestTemporalPgBootstrapDirEnvConstant:
    """Bug #1313 round-3: cross-process IPC-of-a-bootstrap-path contract.

    The server lifespan process installs the PG temporal factory in-process,
    but cluster temporal indexing actually runs in a CHILD `cidx index
    --index-commits` subprocess, which never called
    set_temporal_metadata_backend_factory -- so it silently used the SQLite
    default. TEMPORAL_PG_BOOTSTRAP_DIR_ENV is the env var name the parent
    sets (ONLY in postgres mode, ONLY on the two temporal-index Popen calls)
    carrying the server's bootstrap dir (never the DSN itself -- argv/env are
    world-readable via /proc/<pid>/cmdline, so only a path crosses, and the
    child re-reads storage_mode + postgres_dsn from config.json at that path).
    """

    def test_constant_is_the_expected_literal_env_var_name(self):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        assert TEMPORAL_PG_BOOTSTRAP_DIR_ENV == "CIDX_TEMPORAL_PG_BOOTSTRAP_DIR"
