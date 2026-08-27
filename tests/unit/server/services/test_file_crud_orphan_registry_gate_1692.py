"""
Bug #1692: file_crud_service must gate repo validity on the registry
(ActivatedRepoManager.user_has_activated_repo), not mere filesystem
existence.

Prior to this fix, ``FileCRUDService._resolve_repo_path`` gated repo
validity solely on ``repo_path.exists()`` (the older Bug #394/#395 check).
This meant an "orphan" repository_alias directory -- one that exists on
disk but has NO registry entry (no ``{alias}_metadata.json`` under the
user's activated-repos directory) -- was silently ACCEPTED for writes.

Meanwhile ``auto_watch_manager`` (via
``mcp/handlers/files.py::_start_auto_watch_if_needed``, Bug #1683 round 3)
already uses the registry-backed ``user_has_activated_repo()`` check for
this exact class of problem and correctly REFUSES to watch the same
orphan alias.

This test constructs a REAL ``ActivatedRepoManager`` against a temporary
data directory (no mocking of the code under test, per CLAUDE.md's
Anti-Mock rule) to build a genuine orphan-directory-with-no-registry-entry
scenario, and a genuine properly-registered scenario, then exercises the
real ``FileCRUDService.create_file`` write path against both.

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

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.services.file_crud_service import FileCRUDService

USERNAME = "testuser"


@pytest.fixture
def real_activated_repo_manager():
    """Real ActivatedRepoManager backed by a temp data dir (filesystem mode).

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


def _make_service(manager: ActivatedRepoManager) -> FileCRUDService:
    service = FileCRUDService()
    service.activated_repo_manager = manager
    return service


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
        service = _make_service(real_activated_repo_manager)

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
        service = _make_service(real_activated_repo_manager)

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
