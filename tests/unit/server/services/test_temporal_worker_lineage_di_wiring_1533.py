"""Bug #1533: the temporal worker's golden-repo lineage lookup must read the
SHARED metadata store, not a node-local one it constructs for itself.

WHAT WAS BROKEN
---------------
``_resolve_golden_temporal_context`` built its OWN
``ActivatedRepoManager(data_dir=...)`` outside the DI chain. A standalone
instance has ``_pool is None``, so every metadata read goes to the NODE-LOCAL
JSON/SQLite store. On a clustered (``storage_mode: postgres``) deployment the
real activation row lives in the SHARED PostgreSQL ``activated_repos`` table,
and the node-local store is empty -- so the lookup returned ``None``, which
``_resolve_golden_repo_alias`` legitimately means as "there is no golden
lineage". That all-None context then sent the caller to
``reconstruct_temporal_backend(repo_path, ...)`` -- the activation's own CoW
clone -- which (correctly, per Bug #1529) no longer holds any temporal data.
Net effect on a cluster: HTTP 200 with ZERO results, silently.

This is invisible on solo/SQLite, where the node-local store IS the shared
store -- there is no divergence to expose. The tests below therefore create
the divergence explicitly: the manager that knows about the activation and
the store a standalone construction would reach are DIFFERENT stores.

THE TWO PROPERTIES UNDER TEST
-----------------------------
1. An injected (DI-wired) manager is what the lineage lookup actually uses --
   the worker no longer substitutes a store of its own choosing.
2. In postgres/cluster mode, "I have no DI-wired shared store to read"
   FAILS LOUDLY. It must never be reported as "there is no lineage", because
   that is precisely the all-None context that reads the activation's clone.
   (Bug #1529 finding #2's principle, now applied to the STORE-SELECTION
   half of the same lookup.)

Real infrastructure throughout: real ``ActivatedRepoManager`` instances, real
on-disk activation metadata and clone directories, the real resolution
functions. Nothing about the lookup logic is mocked. The live-PostgreSQL
counterpart of property 1 (a row readable ONLY through a real psycopg pool)
lives in
tests/unit/server/storage/postgres/test_temporal_worker_lineage_live_pg_1533.py.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.services.temporal_worker import (
    TemporalLineageStoreUnavailableError,
    _resolve_golden_temporal_context,
    _resolve_lineage_repo_manager,
)
from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)

USERNAME = "alice"
ACTIVATED_ALIAS = "myclone"
GOLDEN_ALIAS = "evolution"


class _WorkerInput:
    """Only the three attributes the lineage resolver reads."""

    def __init__(self, repo_path: str) -> None:
        self.username = USERNAME
        self.repository_alias = ACTIVATED_ALIAS
        self.repo_path = repo_path


def _install_app_state(monkeypatch: pytest.MonkeyPatch, *, storage_mode) -> None:
    """Substitute a minimal ``code_indexer.server.app`` module exposing only
    ``app.state.storage_mode``.

    Installed via ``sys.modules`` because that is the SAME lookup the
    production code performs (``sys.modules.get(...)`` -- registry_factory's
    established, import-cost-free technique). Importing the real server app
    here would build an entire FastAPI application wired to the developer's
    own ``~/.cidx-server`` data directory.
    """
    module = types.ModuleType("code_indexer.server.app")
    module.app = types.SimpleNamespace(  # type: ignore[attr-defined]
        state=types.SimpleNamespace(storage_mode=storage_mode)
    )
    monkeypatch.setitem(sys.modules, "code_indexer.server.app", module)


def _make_shared_store_manager(shared_data_dir: Path) -> ActivatedRepoManager:
    """A real manager holding a real activation record for (USERNAME,
    ACTIVATED_ALIAS) -- the stand-in for the DI-wired, shared-store manager
    the server itself owns."""
    manager = ActivatedRepoManager(data_dir=str(shared_data_dir))
    clone_dir = Path(manager.get_activated_repo_path(USERNAME, ACTIVATED_ALIAS))
    clone_dir.mkdir(parents=True, exist_ok=True)
    manager._save_metadata_file(
        USERNAME,
        ACTIVATED_ALIAS,
        {
            "user_alias": ACTIVATED_ALIAS,
            "username": USERNAME,
            "golden_repo_alias": GOLDEN_ALIAS,
            "path": str(clone_dir),
            "current_branch": "master",
            "activated_at": "2025-01-01T00:00:00Z",
            "last_accessed": "2025-01-01T00:00:00Z",
        },
    )
    return manager


@pytest.fixture
def diverged_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Two DIFFERENT real stores: the shared one (knows the activation) and
    the node-local one a standalone construction would reach (empty).

    ``CIDX_SERVER_DATA_DIR`` and ``Path.home()`` BOTH point at the empty
    node-local tree, so no accidental agreement can mask the divergence --
    those are the only two directories a standalone
    ``ActivatedRepoManager`` construction can resolve to (Bug #1517).
    """
    node_local_root = tmp_path / "node-local-server-dir"
    (node_local_root / "data").mkdir(parents=True)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(node_local_root))
    monkeypatch.setattr(Path, "home", lambda: node_local_root)

    shared_data_dir = tmp_path / "shared-cluster-store" / "data"
    shared_manager = _make_shared_store_manager(shared_data_dir)
    clone_dir = Path(shared_manager.get_activated_repo_path(USERNAME, ACTIVATED_ALIAS))
    return shared_manager, _WorkerInput(str(clone_dir))


# ---------------------------------------------------------------------------
# Property 1: the DI-wired manager is the one actually consulted
# ---------------------------------------------------------------------------


def test_injected_manager_is_used_for_the_lineage_lookup(diverged_stores) -> None:
    """The activation is known ONLY to the injected manager's store. Resolving
    the lineage must therefore succeed -- and yield the golden repo's FIXED
    temporal root, never an activation-local path."""
    shared_manager, worker_input = diverged_stores

    ctx = _resolve_golden_temporal_context(
        worker_input, "job-1", activated_repo_manager=shared_manager
    )

    assert ctx.alias == GOLDEN_ALIAS, (
        "the lineage must be read from the injected (DI-wired) manager's "
        "store; resolving None here is exactly the cluster defect -- it "
        "produces an all-None context and reads the activation's CoW clone"
    )
    assert ctx.activated_repo_manager is shared_manager, (
        "the SAME injected manager must be carried in the context: "
        "load_golden_temporal_config() reads .golden_repo_manager off it for "
        "temporal embedder selection, and a node-local GoldenRepoManager is "
        "what raises the per-query GoldenRepoNotFoundError on a cluster"
    )
    expected_root = server_temporal_index_root(
        Path(shared_manager.activated_repos_dir).parent / "golden-repos",
        GOLDEN_ALIAS,
    )
    assert ctx.temporal_index_dir == expected_root


def test_node_local_store_cannot_see_the_activation(diverged_stores) -> None:
    """Discriminating counterpart: proves the divergence is real, i.e. that
    the previous test passes because of the injection and not because the
    node-local store happens to contain the record too.

    Without injection (and outside postgres mode, so the loud failure does
    not apply), the standalone node-local construction genuinely finds
    nothing -- the empty-results cluster symptom, reproduced.
    """
    _shared_manager, worker_input = diverged_stores

    ctx = _resolve_golden_temporal_context(worker_input, "job-2")

    assert ctx.alias is None
    assert ctx.temporal_index_dir is None


# ---------------------------------------------------------------------------
# Property 2: postgres mode must fail LOUDLY, never degrade
# ---------------------------------------------------------------------------


def test_postgres_mode_without_an_injected_manager_fails_loudly(
    diverged_stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In cluster mode a node-local store is NEVER the right store to read.
    Silently reading it is what returns zero results, so an un-injected
    lookup must raise instead of producing an all-None context."""
    _shared_manager, worker_input = diverged_stores
    _install_app_state(monkeypatch, storage_mode="postgres")

    with pytest.raises(TemporalLineageStoreUnavailableError):
        _resolve_golden_temporal_context(worker_input, "job-3")


def test_postgres_mode_with_an_unwired_manager_fails_loudly(
    diverged_stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth against a startup-ordering regression: a manager that
    was injected but never received its cluster connection pool still reads
    node-local metadata, so it is just as wrong as no manager at all."""
    shared_manager, worker_input = diverged_stores
    _install_app_state(monkeypatch, storage_mode="postgres")

    assert shared_manager.uses_shared_metadata_stores() is False

    with pytest.raises(TemporalLineageStoreUnavailableError):
        _resolve_golden_temporal_context(
            worker_input, "job-4", activated_repo_manager=shared_manager
        )


def test_postgres_mode_rejects_a_partially_wired_manager(
    diverged_stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager can be HALF wired, and half is not wired.

    The lineage lookup depends on TWO independent stores: activation metadata
    (ActivatedRepoManager's own connection pool) and golden-repo metadata
    (its .golden_repo_manager's backend). A manager holding the activation
    pool but whose GoldenRepoManager self-constructed a NODE-LOCAL SQLite
    backend resolves the lineage correctly and then still raises
    GoldenRepoNotFoundError on the golden lookup -- the exact second symptom
    in this bug's report ("Loaded 0 golden repos from SQLite"), silently
    swallowed by load_golden_temporal_config's fail-open. Checking only the
    activation pool would let that through.
    """
    shared_manager, worker_input = diverged_stores
    _install_app_state(monkeypatch, storage_mode="postgres")

    # A real pool object is irrelevant here: the predicate asks only whether a
    # shared store is wired, and this manager's golden half deliberately is
    # not (its GoldenRepoManager built its own node-local SQLite backend).
    shared_manager.set_connection_pool(object())

    assert shared_manager.golden_repo_manager.has_shared_metadata_backend() is False
    assert shared_manager.uses_shared_metadata_stores() is False

    with pytest.raises(TemporalLineageStoreUnavailableError):
        _resolve_golden_temporal_context(
            worker_input, "job-partial", activated_repo_manager=shared_manager
        )


class _StubPool:
    """Minimal stand-in exposing the one capability the predicate looks for.

    A bare ``object()`` is NOT a connection pool, and accepting one is the
    same "presence is not capability" mistake as accepting any injected
    backend -- so the predicate checks for ``.connection`` and this stub has
    it. Real pooling is exercised by the live-PG module.
    """

    def connection(self):  # pragma: no cover - never called by these tests
        raise NotImplementedError


def _make_manager_with_backend(data_dir: Path, backend) -> ActivatedRepoManager:
    """A real ActivatedRepoManager whose GoldenRepoManager holds *backend*."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    data_dir.mkdir(parents=True, exist_ok=True)
    manager = ActivatedRepoManager(
        data_dir=str(data_dir),
        golden_repo_manager=GoldenRepoManager(
            data_dir=str(data_dir), storage_backend=backend
        ),
    )
    manager.set_connection_pool(_StubPool())
    return manager


def test_postgres_mode_rejects_an_injected_sqlite_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INJECTED is not the same as SHARED.

    A cluster manager whose golden-metadata backend is a node-local SQLite
    one -- however it got there -- reads node-local state, which is the exact
    bug class this guard exists to close. Accepting it merely because
    something was injected would let a miswired cluster node through.
    """
    from code_indexer.server.storage.sqlite_backends import (
        GoldenRepoMetadataSqliteBackend,
    )

    _install_app_state(monkeypatch, storage_mode="postgres")
    data_dir = tmp_path / "sqlite-injected" / "data"
    backend = GoldenRepoMetadataSqliteBackend(str(tmp_path / "golden.db"))
    manager = _make_manager_with_backend(data_dir, backend)

    assert manager.uses_shared_metadata_stores() is False

    with pytest.raises(TemporalLineageStoreUnavailableError):
        _resolve_lineage_repo_manager(manager)


def test_postgres_mode_accepts_genuinely_shared_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REAL PostgreSQL-backed metadata store is accepted.

    Uses the actual GoldenRepoMetadataPostgresBackend class (constructing it
    opens no connection), so the predicate's type/capability check is
    genuinely exercised rather than asserted against a hand-made stub.
    """
    from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
        GoldenRepoMetadataPostgresBackend,
    )
    from code_indexer.server.storage.sqlite_backends import (
        GoldenRepoMetadataSqliteBackend,
    )

    _install_app_state(monkeypatch, storage_mode="postgres")
    data_dir = tmp_path / "shared-stores" / "data"

    # Built with the SQLite backend because GoldenRepoManager.__init__ reads
    # the backend (list_repos) and a PostgreSQL one needs a live pool for
    # that. The real PG backend is then installed directly -- which also pins
    # the intended design: the predicate must ask the CURRENT backend what it
    # is, never trust a "something was injected" flag captured at __init__.
    manager = _make_manager_with_backend(
        data_dir, GoldenRepoMetadataSqliteBackend(str(tmp_path / "golden.db"))
    )
    manager.golden_repo_manager._sqlite_backend = GoldenRepoMetadataPostgresBackend(
        _StubPool()
    )

    assert manager.uses_shared_metadata_stores() is True

    # Assert at the GUARD, not through the full lookup: once a connection pool
    # is set, metadata reads take the PostgreSQL branch, so the lookup cannot
    # be served by this manager's local file store at all. Proving the guard
    # ADMITS a fully-wired manager is the property under test; the real
    # PG-backed lookup is proven in
    # tests/unit/server/storage/postgres/test_temporal_worker_lineage_live_pg_1533.py.
    assert _resolve_lineage_repo_manager(manager) is manager


def _manager_with_backend(tmp_path: Path, backend) -> ActivatedRepoManager:
    """A real manager whose golden backend is *backend* and whose pool is the
    capable `_StubPool`. The backend is installed after construction because
    GoldenRepoManager.__init__ reads it (see the swap rationale above)."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
    from code_indexer.server.storage.sqlite_backends import (
        GoldenRepoMetadataSqliteBackend,
    )

    data_dir = tmp_path / "strictness" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manager = ActivatedRepoManager(
        data_dir=str(data_dir),
        golden_repo_manager=GoldenRepoManager(
            data_dir=str(data_dir),
            storage_backend=GoldenRepoMetadataSqliteBackend(
                str(data_dir / "golden.db")
            ),
        ),
    )
    manager.golden_repo_manager._sqlite_backend = backend
    manager.set_connection_pool(_StubPool())
    return manager


class TestSharedBackendMarkerStrictness:
    """The shared-store marker must be checked EXACTLY, not for truthiness.

    Codex round 5: `bool(getattr(backend, "is_shared_backend", False))` is
    satisfied by ANY truthy value, so a MagicMock -- which fabricates every
    attribute on demand -- passes as "shared" although it never declared the
    marker deliberately. Only the literal True is a deliberate declaration.
    """

    def test_magicmock_backend_is_not_accepted_as_shared(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        manager = _manager_with_backend(tmp_path, MagicMock())

        assert manager.golden_repo_manager.has_shared_metadata_backend() is False
        assert manager.uses_shared_metadata_stores() is False

    def test_truthy_non_true_marker_is_not_accepted_as_shared(
        self, tmp_path: Path
    ) -> None:
        class _DuckTypedBackend:
            is_shared_backend = "yes"  # truthy, but never declared True

        manager = _manager_with_backend(tmp_path, _DuckTypedBackend())

        assert manager.golden_repo_manager.has_shared_metadata_backend() is False
        assert manager.uses_shared_metadata_stores() is False


class TestPoolCapabilityAndMarkerDeclaration:
    """Presence is not capability, and every backend must declare the marker."""

    def test_pool_with_non_callable_connection_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """`hasattr` is presence only: a pool whose `connection` is a plain
        attribute cannot hand out connections."""
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        class _PoolWithNonCallableConnection:
            connection = "not-callable"

        manager = _manager_with_backend(
            tmp_path, GoldenRepoMetadataPostgresBackend(_StubPool())
        )
        manager.set_connection_pool(_PoolWithNonCallableConnection())

        assert manager.uses_shared_metadata_stores() is False

    def test_protocol_and_both_backends_declare_the_marker(self) -> None:
        """A future backend must be forced to state which kind it is, rather
        than silently defaulting into either answer."""
        from code_indexer.server.storage.protocols import GoldenRepoMetadataBackend
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )
        from code_indexer.server.storage.sqlite_backends import (
            GoldenRepoMetadataSqliteBackend,
        )

        assert "is_shared_backend" in getattr(
            GoldenRepoMetadataBackend, "__annotations__", {}
        ), "the Protocol itself must declare is_shared_backend"
        assert GoldenRepoMetadataPostgresBackend.is_shared_backend is True
        assert GoldenRepoMetadataSqliteBackend.is_shared_backend is False


def test_solo_mode_still_uses_the_node_local_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Solo/SQLite (and pure-CLI) behavior is unchanged: there the node-local
    store IS the real store, so the legacy standalone construction -- still
    honoring CIDX_SERVER_DATA_DIR per Bug #1517 -- remains correct."""
    server_root = tmp_path / "configured-server-dir"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(server_root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "unrelated-home")
    _install_app_state(monkeypatch, storage_mode="sqlite")

    manager = _make_shared_store_manager(server_root / "data")
    clone_dir = Path(manager.get_activated_repo_path(USERNAME, ACTIVATED_ALIAS))

    ctx = _resolve_golden_temporal_context(_WorkerInput(str(clone_dir)), "job-5")

    assert ctx.alias == GOLDEN_ALIAS
    assert ctx.temporal_index_dir == server_temporal_index_root(
        Path(manager.activated_repos_dir).parent / "golden-repos", GOLDEN_ALIAS
    )
