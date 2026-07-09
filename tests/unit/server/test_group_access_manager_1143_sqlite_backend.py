"""
Regression test for Bug #1143 + AttributeError in lifespan.py (group-init).

Root cause: GroupAccessManager.__init__ unconditionally calls
  self._backend.bootstrap_default_groups()
on any non-None storage_backend.  In SQLite mode the factory passes a
GroupAccessManager instance as the storage_backend; GroupAccessManager only
has _bootstrap_default_groups() (private), not bootstrap_default_groups() —
so construction raises AttributeError and server startup aborts.

This test reproduces the exact factory.py + lifespan.py scenario:
  1. factory.py creates GroupAccessManager(groups_db_path) — SQLite backend, no storage_backend.
  2. lifespan.py creates GroupAccessManager(groups_db_path, storage_backend=<above instance>).
Step 2 MUST NOT raise, and default groups MUST be seeded.
"""

import tempfile
from pathlib import Path

import pytest

from code_indexer.server.services.group_access_manager import GroupAccessManager


class TestGroupAccessManagerSqliteBackendConstruction:
    """
    Verifies GroupAccessManager construction when the storage_backend is
    a GroupAccessManager (SQLite mode — the scenario used by the e2e in-process
    server path via StorageFactory._create_sqlite_backends).
    """

    @pytest.fixture
    def temp_groups_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        yield db_path
        if db_path.exists():
            db_path.unlink()

    def test_construction_with_sqlite_gam_backend_does_not_raise(
        self, temp_groups_db: Path
    ) -> None:
        """
        RED -> GREEN: GroupAccessManager(db, storage_backend=<GroupAccessManager>)
        must NOT raise AttributeError.

        Reproduces the exact lifespan.py + factory.py interaction:
          - Step 1 (factory): create SQLite GroupAccessManager without storage_backend.
          - Step 2 (lifespan): create a second GroupAccessManager passing the first as
            storage_backend (backend_registry.groups in SQLite mode IS a GroupAccessManager).

        Before the fix this raises:
          AttributeError: 'GroupAccessManager' object has no attribute 'bootstrap_default_groups'
        """
        # Step 1: Factory creates the SQLite-backend GroupAccessManager.
        sqlite_backend_gam = GroupAccessManager(temp_groups_db)

        # Step 2: Lifespan creates the real manager, passing the factory instance as backend.
        # This MUST NOT raise.
        lifespan_gam = GroupAccessManager(
            temp_groups_db, storage_backend=sqlite_backend_gam
        )

        # The lifespan manager must be usable — default groups must exist.
        groups = lifespan_gam.get_all_groups()
        group_names = {g.name for g in groups}
        assert group_names == {"admins", "powerusers", "users"}, (
            f"Default groups must be seeded; found: {group_names}"
        )

    def test_default_groups_seeded_via_sqlite_backend_path(
        self, temp_groups_db: Path
    ) -> None:
        """
        Verifies that the three default groups exist on the SQLite backend after
        the two-step construction (factory -> lifespan).

        Groups ARE seeded by the factory-level GroupAccessManager during its own
        __init__ call (SQLite path calls _bootstrap_default_groups()).  The lifespan
        manager must surface them via its backend delegation — not attempt a second
        bootstrap that would throw.
        """
        sqlite_backend_gam = GroupAccessManager(temp_groups_db)
        lifespan_gam = GroupAccessManager(
            temp_groups_db, storage_backend=sqlite_backend_gam
        )

        admins = lifespan_gam.get_group_by_name("admins")
        powerusers = lifespan_gam.get_group_by_name("powerusers")
        users = lifespan_gam.get_group_by_name("users")

        assert admins is not None, "admins default group must exist"
        assert powerusers is not None, "powerusers default group must exist"
        assert users is not None, "users default group must exist"

        assert admins.is_default, "admins must be flagged as default"
        assert powerusers.is_default, "powerusers must be flagged as default"
        assert users.is_default, "users must be flagged as default"
