"""
Bug #1692: file_crud_service must gate repo validity on the registry
(ActivatedRepoManager.user_has_activated_repo), not mere filesystem
existence -- AND that registry check must consult the DI-wired
(app.state) ActivatedRepoManager, never a node-local, unpooled instance.

Prior to the FIRST fix attempt, ``FileCRUDService._resolve_repo_path``
gated repo validity solely on ``repo_path.exists()`` (the older Bug
#394/#395 check). This meant an "orphan" repository_alias directory --
one that exists on disk but has NO registry entry (no
``{alias}_metadata.json`` under the user's activated-repos directory) --
was silently ACCEPTED for writes.

Meanwhile ``auto_watch_manager`` (via
``mcp/handlers/files.py::_start_auto_watch_if_needed``, Bug #1683 round 3)
already uses the registry-backed ``user_has_activated_repo()`` check for
this exact class of problem and correctly REFUSES to watch the same
orphan alias.

The FIRST fix attempt (commit 851797cf) added the registry check but
consulted it via ``self.activated_repo_manager`` -- the Bug #1689 lazy
property, which constructs a fresh, NODE-LOCAL, UNPOOLED
``ActivatedRepoManager``. That instance's ``user_has_activated_repo()``
falls back to scanning node-local ``{alias}_metadata.json`` files
(``_list_user_repos_fs``); in cluster/PostgreSQL mode, activation writes
to the ``activated_repos`` DB table only (``_save_metadata_pg``) and NEVER
writes that JSON file. So a genuinely activated cluster repo looked
indistinguishable from an orphan to that local instance, and the "fix"
refused 100% of legitimate cluster writes. The corrected fix resolves the
manager via the module-level ``_get_activated_repo_manager()``
(app.state-wired, at call time), matching the established
``stats_service.py`` Bug #1683 pattern and ``auto_watch_manager``'s own
resolution mechanism.

This test module constructs a REAL ``ActivatedRepoManager`` against a
temporary data directory (no mocking of the code under test, per
CLAUDE.md's Anti-Mock rule) for the filesystem/solo-mode scenarios, wired
onto ``app.state`` (the seam ``_resolve_repo_path`` now reads), and uses a
MagicMock standing in for a cluster-mode manager for the discriminating
test below (mirroring ``test_stats_service_uses_app_state_1683.py``'s own
sentinel-manager pattern for the identical bug class).

Before/after reproduction evidence for the pre-fix buggy acceptance was
captured via an ad-hoc script (not committed -- a permanent test asserting
the pre-fix "accepts" behavior would collide with the fixed-behavior
assertion below and permanently fail once the fix lands) and is reported
in the bug's resolution notes.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server import app as app_module
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.services.file_crud_service import FileCRUDService

USERNAME = "testuser"
_UNSET = object()


@pytest.fixture
def wired_app_state_activated_repo_manager():
    """Save/restore ``app.state.activated_repo_manager`` around a test and
    provide a helper to install a manager onto it.

    Bug #1692: ``_resolve_repo_path`` must resolve the DI-wired
    (app.state) manager, never a locally-constructed one -- mirrors
    ``test_stats_service_uses_app_state_1683.py``'s identical fixture for
    the same underlying bug class.
    """
    saved = getattr(app_module.app.state, "activated_repo_manager", _UNSET)

    def _install(manager):
        app_module.app.state.activated_repo_manager = manager
        return manager

    yield _install

    if saved is _UNSET:
        if hasattr(app_module.app.state, "activated_repo_manager"):
            delattr(app_module.app.state, "activated_repo_manager")
    else:
        app_module.app.state.activated_repo_manager = saved


@pytest.fixture
def real_activated_repo_manager(wired_app_state_activated_repo_manager):
    """Real ActivatedRepoManager backed by a temp data dir (filesystem
    mode), wired onto app.state -- the seam _resolve_repo_path now reads.

    golden_repo_manager/background_job_manager are substituted with
    MagicMock() -- neither is touched by the filesystem-scan methods under
    test (_list_user_repos_fs, get_activated_repo_path), so this is a
    dependency substitution (not mocking the code under test) that avoids
    spinning up a real SQLite-backed GoldenRepoManager for an unrelated
    dependency.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=MagicMock(),
            background_job_manager=MagicMock(),
        )
        wired_app_state_activated_repo_manager(manager)
        yield manager


def _make_orphan_repo_dir(manager: ActivatedRepoManager, alias: str) -> str:
    """Create a repo directory on disk with NO registry (_metadata.json) entry."""
    repo_dir = os.path.join(manager.activated_repos_dir, USERNAME, alias)
    os.makedirs(repo_dir, exist_ok=True)
    return repo_dir


def _make_registered_repo_dir(manager: ActivatedRepoManager, alias: str) -> str:
    """Create a repo directory on disk AND a matching _metadata.json registry entry."""
    user_dir = os.path.join(manager.activated_repos_dir, USERNAME)
    os.makedirs(user_dir, exist_ok=True)
    repo_dir = os.path.join(user_dir, alias)
    os.makedirs(repo_dir, exist_ok=True)
    metadata_path = os.path.join(user_dir, f"{alias}_metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(
            {
                "user_alias": alias,
                "golden_repo_alias": "some-golden-repo",
            },
            f,
        )
    return repo_dir


class TestOrphanDirectoryRegistryGate:
    """Bug #1692: orphan directory (exists on disk, no registry entry)."""

    def test_create_file_refuses_orphan_directory_with_no_registry_entry(
        self, real_activated_repo_manager
    ):
        """Fixed behavior (Bug #1692): create_file must REFUSE a write into
        an orphan directory that exists on disk but has no registry entry
        -- matching auto_watch_manager's registry-backed standard instead
        of accepting it via a bare filesystem-existence check.
        """
        alias = "orphan-repo"
        _make_orphan_repo_dir(real_activated_repo_manager, alias)
        service = FileCRUDService()

        with pytest.raises(FileNotFoundError) as exc_info:
            service.create_file(
                repo_alias=alias,
                file_path="new_file.py",
                content="print('should not land')",
                username=USERNAME,
            )

        assert alias in str(exc_info.value)

        # The delta must NOT have landed in the unregistered directory.
        repo_dir = os.path.join(
            real_activated_repo_manager.activated_repos_dir, USERNAME, alias
        )
        assert not (Path(repo_dir) / "new_file.py").exists()

    def test_create_file_still_succeeds_for_properly_registered_activated_repo(
        self, real_activated_repo_manager
    ):
        """Regression guard: a genuinely registered activated repo (real
        registry entry + real directory) must continue to accept writes.
        """
        alias = "registered-repo"
        repo_dir = _make_registered_repo_dir(real_activated_repo_manager, alias)
        service = FileCRUDService()

        result = service.create_file(
            repo_alias=alias,
            file_path="new_file.py",
            content="print('hello')",
            username=USERNAME,
        )

        assert result["success"] is True
        created_file = Path(repo_dir) / "new_file.py"
        assert created_file.exists()
        assert created_file.read_text() == "print('hello')"


class TestClusterModeConsultsDIWiredManager:
    """Bug #1692 (rejected first fix attempt): the registry check must
    consult the DI-wired (app.state) manager, never a node-local, unpooled
    instance -- otherwise a genuinely activated CLUSTER repo (real PG row,
    real CoW clone, but NO local {alias}_metadata.json, since PG-mode
    activation never writes one) is indistinguishable on disk from a true
    orphan, and gets wrongly refused.
    """

    def test_create_file_accepted_when_di_wired_manager_reports_activated_despite_no_local_metadata(
        self, wired_app_state_activated_repo_manager, tmp_path
    ):
        """This is the discriminating test that would have caught the
        rejected first fix attempt: a directory that exists on disk with
        NO {alias}_metadata.json (the exact on-disk shape of BOTH a true
        orphan AND a genuine cluster-mode activation) must be ACCEPTED
        when the DI-wired manager reports the alias as activated -- e.g.
        via its PostgreSQL connection pool, simulated here with a
        MagicMock sentinel manager (mirrors
        test_stats_service_uses_app_state_1683.py's identical technique
        for the same bug class).

        Run against the rejected commit (which read
        self.activated_repo_manager -- a node-local instance never wired
        to this sentinel), this test fails: the local instance's own
        filesystem scan finds no {alias}_metadata.json for this alias and
        refuses the write with FileNotFoundError.
        """
        alias = "cluster-repo"
        repo_dir = tmp_path / "cluster-repo"
        repo_dir.mkdir()
        # Deliberately NO {alias}_metadata.json anywhere -- this directory
        # is not under any real ActivatedRepoManager's activated_repos_dir
        # at all, so a node-local filesystem scan could never find it
        # registered regardless of directory layout.

        wired_manager = MagicMock(name="di-wired-cluster-activated-repo-manager")
        wired_manager.user_has_activated_repo.return_value = True
        wired_manager.get_activated_repo_path.return_value = str(repo_dir)
        wired_app_state_activated_repo_manager(wired_manager)

        service = FileCRUDService()
        result = service.create_file(
            repo_alias=alias,
            file_path="new_file.py",
            content="print('cluster hello')",
            username="alice",
        )

        assert result["success"] is True
        wired_manager.user_has_activated_repo.assert_called_once_with("alice", alias)
        created_file = repo_dir / "new_file.py"
        assert created_file.exists()
        assert created_file.read_text() == "print('cluster hello')"

    def test_resolve_repo_path_raises_runtime_error_when_app_state_manager_unwired(
        self, wired_app_state_activated_repo_manager
    ):
        """Fail loud (RuntimeError), never silently substitute a
        locally-constructed manager, when app.state has no wired
        ActivatedRepoManager at all.
        """
        if hasattr(app_module.app.state, "activated_repo_manager"):
            delattr(app_module.app.state, "activated_repo_manager")

        service = FileCRUDService()

        with pytest.raises(RuntimeError, match="activated_repo_manager"):
            service.create_file(
                repo_alias="whatever-repo",
                file_path="new_file.py",
                content="irrelevant",
                username="alice",
            )
