"""Unit tests for the cidx-meta global-registration self-heal repair.

Root cause (E2E-discovered via Phase 6 PostgreSQL Parity log-audit gate):
``bootstrap_cidx_meta()`` runs inside ``initialize_services()``, which is
called at ``app.py`` MODULE level -- BEFORE the FastAPI ``app`` object is
even assigned in ``code_indexer.server.app``. At that point,
``GlobalActivator.registry`` resolves via
``resolve_backend_registry_attr()``, which inspects
``sys.modules["code_indexer.server.app"].app.state`` to decide whether the
process is running in postgres/cluster mode. Since the ``app`` object does
not exist yet, ``_running_server_app_state()`` returns ``None``, and the
resolver -- unable to distinguish "definitely solo/SQLite mode" from "too
early to tell" -- silently falls back to a per-node SQLite-backed
``GlobalRegistry``.  cidx-meta's very first "-global" registration is
written into that per-node SQLite fallback instead of the shared
PostgreSQL ``global_repos`` table, in EVERY fresh postgres/cluster
deployment.  Every later reader (``RefreshScheduler.registry`` used by
``meta_description_hook`` and the dep-map trigger) correctly resolves the
SHARED PostgreSQL registry (since by then ``app.state.backend_registry`` is
populated) and reports "Repository 'cidx-meta-global' not found in global
registry" -- confirmed live in a real Phase 6 PG-parity server.log:
    "Registered global repo (SQLite): cidx-meta-global"   (bootstrap-time write)
    "... not found in global registry"                    (every later read)

``repair_cidx_meta_global_registration()`` is a small, idempotent, O(1)
self-heal called from ``lifespan.py`` AFTER ``app.state.backend_registry``
is guaranteed populated (mirroring the project's existing self-heal
patterns -- Bug #1317/#1523 reconciliation). It detects "cidx-meta is a
registered golden repo but its '-global' entry is missing from the
NOW-correctly-resolvable registry" and re-runs the global activation.

Mock justification: ``golden_repo_manager`` is a MagicMock here because
these tests exercise ONLY the repair function's own control flow
(registered? already-global? repair-and-verify), not GoldenRepoManager's
internals -- the exact same convention already used by
``test_bootstrap_cidx_meta_excludes.py`` for the same collaborator.
``GlobalActivator`` itself is REAL, backed by a REAL on-disk SQLite
``GlobalRegistry`` -- no mocking of the code under test. The registry's
``global_repos`` table is created via the real ``DatabaseSchema`` schema
initializer (the same one production uses), not hand-rolled SQL.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.global_activation import GlobalActivator
from code_indexer.server.startup.bootstrap import (
    repair_cidx_meta_global_registration,
)
from code_indexer.server.storage.database_manager import DatabaseSchema


def _make_manager(golden_repo_registered: bool) -> MagicMock:
    mgr = MagicMock()
    mgr.golden_repo_exists.return_value = golden_repo_registered
    return mgr


@pytest.fixture()
def golden_repos_dir(tmp_path: Path) -> Path:
    """A golden-repos dir with its sibling ``cidx_server.db`` schema ready.

    GlobalActivator's SQLite fallback (get_server_global_registry) derives
    db_path as ``golden_repos_dir.parent / "cidx_server.db"`` -- placing
    golden_repos UNDER tmp_path (not AT tmp_path) keeps that derived path
    isolated to this one test's own tmp_path, never a shared pytest
    session-level parent directory.
    """
    repos_dir = tmp_path / "golden-repos"
    repos_dir.mkdir(parents=True)
    db_path = tmp_path / "cidx_server.db"
    DatabaseSchema(db_path=str(db_path)).initialize_database()
    return repos_dir


class TestNotYetRegistered:
    def test_returns_false_and_does_nothing_when_cidx_meta_not_registered(
        self, golden_repos_dir: Path
    ) -> None:
        """cidx-meta isn't a golden repo yet -- repair is a pure no-op."""
        manager = _make_manager(golden_repo_registered=False)

        result = repair_cidx_meta_global_registration(manager, str(golden_repos_dir))

        assert result is False
        # No alias pointer should have been created.
        assert not (golden_repos_dir / "aliases" / "cidx-meta-global.json").exists()


class TestAlreadyGlobal:
    def test_returns_false_when_global_entry_already_present(
        self, golden_repos_dir: Path
    ) -> None:
        """cidx-meta already has a healthy '-global' entry -- no-op, idempotent."""
        cidx_meta_path = golden_repos_dir / "cidx-meta"
        cidx_meta_path.mkdir(parents=True)

        # Simulate a HEALTHY prior activation (the success path).
        pre_activator = GlobalActivator(str(golden_repos_dir))
        pre_activator.activate_golden_repo(
            repo_name="cidx-meta",
            repo_url="local://cidx-meta",
            clone_path=str(cidx_meta_path),
            enable_temporal=False,
            temporal_options=None,
        )

        manager = _make_manager(golden_repo_registered=True)

        result = repair_cidx_meta_global_registration(manager, str(golden_repos_dir))

        assert result is False


class TestMissingGlobalEntryIsRepaired:
    def test_repairs_missing_global_entry_for_registered_cidx_meta(
        self, golden_repos_dir: Path
    ) -> None:
        """The exact bug scenario: golden repo row exists, '-global' entry
        does not -- repair must create it so later readers can resolve
        'cidx-meta-global' via the registry.
        """
        cidx_meta_path = golden_repos_dir / "cidx-meta"
        cidx_meta_path.mkdir(parents=True)

        manager = _make_manager(golden_repo_registered=True)

        # Precondition: nothing registered globally yet (the bug state).
        pre_check = GlobalActivator(str(golden_repos_dir))
        assert pre_check.registry.get_global_repo("cidx-meta-global") is None

        result = repair_cidx_meta_global_registration(manager, str(golden_repos_dir))

        assert result is True

        # Postcondition: a fresh registry lookup (mirroring a later reader,
        # e.g. RefreshScheduler.registry) now finds the entry.
        post_check = GlobalActivator(str(golden_repos_dir))
        repaired = post_check.registry.get_global_repo("cidx-meta-global")
        assert repaired is not None
        assert repaired["index_path"] == str(cidx_meta_path)

        # The alias pointer file exists too (repair uses the standard
        # activation path, so both halves land together).
        assert (golden_repos_dir / "aliases" / "cidx-meta-global.json").exists()

    def test_idempotent_second_call_is_a_no_op(self, golden_repos_dir: Path) -> None:
        """Calling repair twice must not raise or duplicate registrations."""
        cidx_meta_path = golden_repos_dir / "cidx-meta"
        cidx_meta_path.mkdir(parents=True)
        manager = _make_manager(golden_repo_registered=True)

        first = repair_cidx_meta_global_registration(manager, str(golden_repos_dir))
        second = repair_cidx_meta_global_registration(manager, str(golden_repos_dir))

        assert first is True
        assert second is False
