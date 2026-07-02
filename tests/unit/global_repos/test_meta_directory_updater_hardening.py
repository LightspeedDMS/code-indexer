"""
Tests for MetaDirectoryUpdater hardening changes.

Covers:
- CHANGE 1+2: Safety threshold (MetaDirectoryMassDeleteBlocked), stub guard, lock discipline
- CHANGE 2: Managed-file filter (registry-aware)
- CHANGE 3: Lock discipline in on_repo_removed
- CHANGE 4: Dead raw writer gate (_update_description_file raises NotImplementedError)
- CHANGE 6: Force-push removal in CidxMetaBackupBootstrap
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cidx_meta_path():
    """Temporary cidx-meta directory."""
    tmp = tempfile.mkdtemp()
    p = Path(tmp)
    yield p
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def registry_with_repos():
    """Factory: returns a mock registry with given alias_name list."""

    def _make(alias_names):
        registry = MagicMock()
        registry.list_global_repos.return_value = [
            {
                "alias_name": a,
                "repo_name": a.replace("-global", ""),
                "repo_url": "https://example.com",
            }
            for a in alias_names
        ]
        return registry

    return _make


@pytest.fixture
def mock_scheduler():
    """A mock refresh_scheduler with acquire/release_write_lock methods."""
    scheduler = MagicMock()
    scheduler.acquire_write_lock.return_value = True
    scheduler.release_write_lock.return_value = None
    return scheduler


def _make_git_env(home_dir):
    return {
        "PATH": "/usr/bin:/bin",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
        "HOME": str(home_dir),
        "GIT_CONFIG_NOSYSTEM": "1",
    }


@pytest.fixture
def git_repo(tmp_path):
    """Real git repo with initial commit, ready for sync tests."""
    repo = tmp_path / "cidx-meta"
    repo.mkdir()
    env = _make_git_env(tmp_path)

    def _git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env=env,
        )

    _git("init")
    _git("config", "user.email", "test@test.com")
    _git("config", "user.name", "test")
    _git("config", "commit.gpgsign", "false")
    (repo / "seed.md").write_text("seed\n")
    _git("add", "-A")
    _git("commit", "-m", "initial")
    return repo


@pytest.fixture
def bare_remote(tmp_path):
    """Bare git remote for bootstrap/push tests."""
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        capture_output=True,
    )
    return remote


# ---------------------------------------------------------------------------
# CLASS 1: Safety threshold — MetaDirectoryMassDeleteBlocked
# ---------------------------------------------------------------------------


class TestSafetyThreshold:
    """CHANGE 1: update() raises MetaDirectoryMassDeleteBlocked when deletion ratio > 50%."""

    def test_exception_class_exists(self):
        """MetaDirectoryMassDeleteBlocked must be importable from meta_directory_updater."""
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryMassDeleteBlocked,
        )

        assert issubclass(MetaDirectoryMassDeleteBlocked, RuntimeError)

    def test_constants_exist(self):
        """MAX_DELETE_RATIO and MIN_FILES_FOR_THRESHOLD must be importable."""
        from code_indexer.global_repos.meta_directory_updater import (
            MAX_DELETE_RATIO,
            MIN_FILES_FOR_THRESHOLD,
        )

        assert MAX_DELETE_RATIO == 0.5
        assert MIN_FILES_FOR_THRESHOLD == 3

    def test_mass_delete_raises_when_ratio_exceeds_threshold(
        self, cidx_meta_path, registry_with_repos
    ):
        """
        Scenario: 4 managed files exist (matched by 4 registry aliases), registry shrinks to 1.
        Deletion ratio = 3/4 = 75% > 50% -> must raise MetaDirectoryMassDeleteBlocked.

        With registry-based file detection, managed files are only those whose stems
        match a known alias. To detect orphaned files the registry must contain aliases
        (even if reduced). A registry that goes from 4->1 with 3 orphaned files = 75%.
        """
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryMassDeleteBlocked,
            MetaDirectoryUpdater,
        )

        # Files use short alias form (no -global suffix); known_aliases=None fallback
        # in update() detects all plausible managed .md files on disk.
        for name in ["a", "b", "c", "d"]:
            (cidx_meta_path / f"{name}.md").write_text(f"# {name}\n")

        registry = registry_with_repos([])
        updater = MetaDirectoryUpdater(str(cidx_meta_path), registry)

        with pytest.raises(MetaDirectoryMassDeleteBlocked) as exc_info:
            updater.update()

        e = exc_info.value
        assert e.to_delete_count == 4
        assert e.existing_count == 4

    def test_mass_delete_exception_carries_correct_fields(
        self, cidx_meta_path, registry_with_repos
    ):
        """MetaDirectoryMassDeleteBlocked fields populated correctly for 2-of-3 case."""
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryMassDeleteBlocked,
            MetaDirectoryUpdater,
        )

        # Files use short alias form; known_aliases=None fallback finds all plausible files
        for name in ["x", "y", "z"]:
            (cidx_meta_path / f"{name}.md").write_text(f"# {name}\n")

        # Only keep one in registry — 2 of 3 deleted = 66% > 50%
        registry = registry_with_repos(["x-global"])
        updater = MetaDirectoryUpdater(str(cidx_meta_path), registry)

        with pytest.raises(MetaDirectoryMassDeleteBlocked) as exc_info:
            updater.update()

        e = exc_info.value
        assert e.to_delete_count == 2
        assert e.existing_count == 3
        assert "y" in e.aliases or "z" in e.aliases

    def test_no_raise_when_below_threshold(self, cidx_meta_path, registry_with_repos):
        """
        Scenario: 4 managed files, 1 orphaned -> ratio=25% < 50% -> no raise.
        """
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        # Files use short alias form; known_aliases=None fallback finds all plausible files
        for name in ["a", "b", "c", "d"]:
            (cidx_meta_path / f"{name}.md").write_text(f"# {name}\n")

        # Remove only one (25%); registry strips -global suffix to get short aliases
        registry = registry_with_repos(["a-global", "b-global", "c-global"])
        updater = MetaDirectoryUpdater(str(cidx_meta_path), registry)
        updater.update()  # Must not raise

        assert not (cidx_meta_path / "d.md").exists()
        assert (cidx_meta_path / "a.md").exists()

    def test_no_raise_when_below_min_files_threshold(
        self, cidx_meta_path, registry_with_repos
    ):
        """
        Scenario: Only 2 managed files exist (< MIN_FILES_FOR_THRESHOLD=3).
        Even if all are orphaned, no MetaDirectoryMassDeleteBlocked raised.
        """
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        for name in ["a", "b"]:
            (cidx_meta_path / f"{name}.md").write_text(f"# {name}\n")

        registry = registry_with_repos([])
        updater = MetaDirectoryUpdater(str(cidx_meta_path), registry)
        updater.update()  # Must not raise — below MIN_FILES_FOR_THRESHOLD

        assert not (cidx_meta_path / "a.md").exists()
        assert not (cidx_meta_path / "b.md").exists()

    def test_files_preserved_after_mass_delete_blocked(
        self, cidx_meta_path, registry_with_repos
    ):
        """After MetaDirectoryMassDeleteBlocked raised, existing files must still be on disk."""
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryMassDeleteBlocked,
            MetaDirectoryUpdater,
        )

        for name in ["a", "b", "c", "d"]:
            (cidx_meta_path / f"{name}.md").write_text(f"# {name}\n")

        registry = registry_with_repos([])
        updater = MetaDirectoryUpdater(str(cidx_meta_path), registry)

        with pytest.raises(MetaDirectoryMassDeleteBlocked):
            updater.update()

        # All files must still exist — raise must happen BEFORE any unlink
        for name in ["a", "b", "c", "d"]:
            assert (cidx_meta_path / f"{name}.md").exists(), (
                f"File {name}.md was deleted despite mass-delete block"
            )


# ---------------------------------------------------------------------------
# CLASS 2: Stub guard — don't overwrite existing files
# ---------------------------------------------------------------------------


class TestStubGuard:
    """CHANGE 1: update() must NOT overwrite existing files when creating stubs."""

    def test_existing_file_not_overwritten_by_stub(
        self, cidx_meta_path, registry_with_repos
    ):
        """If alias is registered and file already exists, update() must not overwrite it."""
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        rich_content = "# MyRepo\n\nRich description from Claude CLI.\n"
        # INVARIANT: cidx-meta filenames use SHORT alias (MyRepo.md), NOT MyRepo-global.md
        (cidx_meta_path / "MyRepo.md").write_text(rich_content)

        registry = registry_with_repos(["MyRepo-global"])
        updater = MetaDirectoryUpdater(str(cidx_meta_path), registry)
        updater.update()

        assert (cidx_meta_path / "MyRepo.md").read_text() == rich_content, (
            "update() overwrote existing file with stub — stub guard failed"
        )

    def test_stub_created_when_file_missing(self, cidx_meta_path, registry_with_repos):
        """update() creates stub only when file does not yet exist."""
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        registry = registry_with_repos(["NewRepo-global"])
        updater = MetaDirectoryUpdater(str(cidx_meta_path), registry)
        updater.update()

        # INVARIANT: cidx-meta filenames use SHORT alias (NewRepo.md), NOT NewRepo-global.md
        expected = cidx_meta_path / "NewRepo.md"
        assert expected.exists()
        assert "NewRepo" in expected.read_text()


# ---------------------------------------------------------------------------
# CLASS 3: Lock discipline in MetaDirectoryUpdater.update()
# ---------------------------------------------------------------------------


class TestUpdateLockDiscipline:
    """CHANGE 1: update() acquires write lock when refresh_scheduler provided."""

    def test_lock_acquired_and_released_on_success(
        self, cidx_meta_path, registry_with_repos, mock_scheduler
    ):
        """When refresh_scheduler is set, acquire_write_lock called then release_write_lock."""
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        registry = registry_with_repos(["repo1-global"])
        updater = MetaDirectoryUpdater(
            str(cidx_meta_path), registry, refresh_scheduler=mock_scheduler
        )
        updater.update()

        mock_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="meta_directory_updater"
        )
        mock_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="meta_directory_updater"
        )

    def test_lock_released_even_when_mass_delete_raised(
        self, cidx_meta_path, registry_with_repos, mock_scheduler
    ):
        """release_write_lock called in finally even when MetaDirectoryMassDeleteBlocked raised."""
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryMassDeleteBlocked,
            MetaDirectoryUpdater,
        )

        for name in ["a", "b", "c", "d"]:
            (cidx_meta_path / f"{name}.md").write_text(f"# {name}\n")

        registry = registry_with_repos([])
        updater = MetaDirectoryUpdater(
            str(cidx_meta_path), registry, refresh_scheduler=mock_scheduler
        )

        with pytest.raises(MetaDirectoryMassDeleteBlocked):
            updater.update()

        mock_scheduler.release_write_lock.assert_called_once()

    def test_no_lock_when_scheduler_not_provided(
        self, cidx_meta_path, registry_with_repos
    ):
        """When refresh_scheduler=None (default), no lock calls, update() works normally."""
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        registry = registry_with_repos(["repo1-global"])
        updater = MetaDirectoryUpdater(str(cidx_meta_path), registry)
        updater.update()

        # INVARIANT: cidx-meta filenames use SHORT alias (repo1.md), NOT repo1-global.md
        expected = cidx_meta_path / "repo1.md"
        assert expected.exists()


# ---------------------------------------------------------------------------
# CLASS 4: Managed-file filter (registry-aware)
# ---------------------------------------------------------------------------


class TestManagedFileFilter:
    """CHANGE 2: _get_existing_description_aliases uses registry-aware filtering."""

    def test_readme_and_underscore_files_not_treated_as_managed(
        self, cidx_meta_path, registry_with_repos
    ):
        """README.md (uppercase initial) and _internal.md (underscore prefix) must not be
        touched or counted as managed files. Plain lowercase files like notes.md ARE
        treated as managed and may be deleted if not in the registry."""
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        (cidx_meta_path / "README.md").write_text("# Readme\n")
        (cidx_meta_path / "_internal.md").write_text("# Internal\n")
        # INVARIANT: cidx-meta filenames use SHORT alias (MyRepo.md), NOT MyRepo-global.md
        (cidx_meta_path / "MyRepo.md").write_text("# MyRepo\nRich.\n")

        registry = registry_with_repos(["MyRepo-global"])
        updater = MetaDirectoryUpdater(str(cidx_meta_path), registry)
        updater.update()

        assert (cidx_meta_path / "README.md").exists()
        assert (cidx_meta_path / "_internal.md").exists()
        assert (cidx_meta_path / "MyRepo.md").exists()
        assert (cidx_meta_path / "README.md").read_text() == "# Readme\n"
        assert (cidx_meta_path / "_internal.md").read_text() == "# Internal\n"

    def test_non_global_md_not_counted_in_existing_set(
        self, cidx_meta_path, registry_with_repos
    ):
        """Non-repo .md files must not appear in _get_existing_description_aliases().

        The registry-based filter requires known_aliases to be passed so that
        README.md, CHANGELOG.md etc. are excluded from the managed set.
        """
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        (cidx_meta_path / "README.md").write_text("# Readme\n")
        (cidx_meta_path / "CHANGELOG.md").write_text("# Changelog\n")

        registry = registry_with_repos([])
        updater = MetaDirectoryUpdater(str(cidx_meta_path), registry)
        # Pass known_aliases=set() to use registry-based filter (no known repos)
        existing = updater._get_existing_description_aliases(known_aliases=set())

        assert "README" not in existing
        assert "CHANGELOG" not in existing


# ---------------------------------------------------------------------------
# CLASS 5: on_repo_removed lock discipline
# ---------------------------------------------------------------------------


class TestOnRepoRemovedLockDiscipline:
    """CHANGE 3: on_repo_removed acquires/releases write lock when _refresh_scheduler set."""

    def test_lock_acquired_and_released_on_unlink(self, tmp_path):
        """When _refresh_scheduler set, lock acquired before unlink and released after."""
        import code_indexer.global_repos.meta_description_hook as hook_module
        from code_indexer.global_repos.meta_description_hook import on_repo_removed

        golden_repos_dir = tmp_path / "golden-repos"
        cidx_meta = golden_repos_dir / "cidx-meta"
        cidx_meta.mkdir(parents=True)
        md_file = cidx_meta / "MyRepo.md"
        md_file.write_text("# MyRepo\n")

        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True
        mock_scheduler.release_write_lock.return_value = None

        original = hook_module._refresh_scheduler
        try:
            hook_module._refresh_scheduler = mock_scheduler
            on_repo_removed("MyRepo", str(golden_repos_dir))
        finally:
            hook_module._refresh_scheduler = original

        assert not md_file.exists(), "md_file should have been deleted"
        mock_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="lifecycle_writer"
        )
        mock_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="lifecycle_writer"
        )

    def test_lock_released_even_when_unlink_fails(self, tmp_path):
        """release_write_lock called in finally even if unlink raises an exception."""
        import code_indexer.global_repos.meta_description_hook as hook_module
        from code_indexer.global_repos.meta_description_hook import on_repo_removed

        golden_repos_dir = tmp_path / "golden-repos"
        cidx_meta = golden_repos_dir / "cidx-meta"
        cidx_meta.mkdir(parents=True)
        md_file = cidx_meta / "MyRepo.md"
        md_file.write_text("# MyRepo\n")

        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True
        mock_scheduler.release_write_lock.return_value = None

        original = hook_module._refresh_scheduler
        try:
            hook_module._refresh_scheduler = mock_scheduler
            with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
                on_repo_removed("MyRepo", str(golden_repos_dir))
        finally:
            hook_module._refresh_scheduler = original

        # Lock must be released even though unlink failed
        mock_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="lifecycle_writer"
        )

    def test_on_repo_removed_skips_delete_when_lock_not_acquired(self, tmp_path):
        """When scheduler is provided but acquire_write_lock returns False,
        on_repo_removed() must NOT delete the .md file."""
        import code_indexer.global_repos.meta_description_hook as hook_module
        from code_indexer.global_repos.meta_description_hook import on_repo_removed

        golden_repos_dir = tmp_path / "golden-repos"
        cidx_meta = golden_repos_dir / "cidx-meta"
        cidx_meta.mkdir(parents=True)
        md_file = cidx_meta / "MyRepo.md"
        md_file.write_text("# MyRepo\n")

        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = False
        mock_scheduler.release_write_lock.return_value = None

        original = hook_module._refresh_scheduler
        try:
            hook_module._refresh_scheduler = mock_scheduler
            on_repo_removed("MyRepo", str(golden_repos_dir))
        finally:
            hook_module._refresh_scheduler = original

        # File must NOT have been deleted (lock was not acquired)
        assert md_file.exists(), (
            "on_repo_removed() deleted the file despite lock not being acquired — "
            "it must skip deletion when lock returns False"
        )
        # release_write_lock must NOT have been called (lock was never acquired)
        mock_scheduler.release_write_lock.assert_not_called()


# ---------------------------------------------------------------------------
# CLASS 6: Dead raw writer gate
# ---------------------------------------------------------------------------


class TestDeadRawWriter:
    """CHANGE 4: _update_description_file raises NotImplementedError."""

    def test_update_description_file_raises_not_implemented(self):
        """DescriptionRefreshScheduler._update_description_file must raise NotImplementedError."""
        from code_indexer.server.services.description_refresh_scheduler import (
            DescriptionRefreshScheduler,
        )

        # Instantiate via object.__new__ to bypass heavy __init__
        instance = object.__new__(DescriptionRefreshScheduler)

        with pytest.raises(NotImplementedError) as exc_info:
            instance._update_description_file("some-alias", "some content")

        assert "deprecated" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# CLASS 8: Force-push removal in CidxMetaBackupBootstrap
# ---------------------------------------------------------------------------


class TestBootstrapNoForcePush:
    """CHANGE 6: CidxMetaBackupBootstrap._push() never force-pushes."""

    def test_push_method_renamed_no_force_fallback(self):
        """_push_with_fallback must no longer exist; _push must exist instead."""
        from code_indexer.server.services.cidx_meta_backup.bootstrap import (
            CidxMetaBackupBootstrap,
        )

        bootstrap = CidxMetaBackupBootstrap()
        assert not hasattr(bootstrap, "_push_with_fallback"), (
            "_push_with_fallback still exists — force-push fallback not removed"
        )
        assert hasattr(bootstrap, "_push"), (
            "_push method missing from CidxMetaBackupBootstrap"
        )

    def test_push_raises_on_rejection(self, git_repo):
        """_push raises RuntimeError when remote rejects push (non-existent remote)."""
        from code_indexer.server.services.cidx_meta_backup.bootstrap import (
            CidxMetaBackupBootstrap,
        )

        bootstrap = CidxMetaBackupBootstrap()
        # Use a non-existent remote path — push will fail
        with pytest.raises(RuntimeError) as exc_info:
            bootstrap._push(str(git_repo), "master")

        assert len(str(exc_info.value)) > 0

    def test_bootstrap_uses_push_not_force_push(self, tmp_path, bare_remote):
        """bootstrap() on a fresh (non-git) directory returns 'bootstrapped' via _push."""
        from code_indexer.server.services.cidx_meta_backup.bootstrap import (
            CidxMetaBackupBootstrap,
        )

        # fresh_dir has no .git — bootstrap() will init + push (not already_initialized)
        fresh_dir = tmp_path / "fresh-cidx-meta"
        fresh_dir.mkdir()
        (fresh_dir / "seed.md").write_text("seed\n")

        result = CidxMetaBackupBootstrap().bootstrap(
            str(fresh_dir), bare_remote.as_uri()
        )
        assert result == "bootstrapped"


# ---------------------------------------------------------------------------
# CLASS 10: Lock discipline — skip when lock not acquired (H3)
# ---------------------------------------------------------------------------


class TestUpdateSkipsWhenLockNotAcquired:
    """H3: update() must skip filesystem changes when scheduler provided but lock not acquired."""

    def test_update_skips_when_lock_returns_false(
        self, cidx_meta_path, registry_with_repos
    ):
        """
        When refresh_scheduler is provided but acquire_write_lock returns False,
        update() must return early without creating any files.
        """
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        registry = registry_with_repos(["new-repo-global"])
        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = False

        updater = MetaDirectoryUpdater(
            str(cidx_meta_path), registry, refresh_scheduler=mock_scheduler
        )
        updater.update()

        # File must NOT have been created (update was skipped); short alias = new-repo.md
        assert not (cidx_meta_path / "new-repo.md").exists(), (
            "update() created files despite lock not being acquired — "
            "it must skip filesystem changes when lock returns False"
        )
        # release_write_lock must NOT have been called (lock was never acquired)
        mock_scheduler.release_write_lock.assert_not_called()

    def test_update_skips_when_lock_raises_exception(
        self, cidx_meta_path, registry_with_repos
    ):
        """
        When refresh_scheduler is provided but acquire_write_lock raises an exception,
        update() must return early without creating any files.
        """
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        registry = registry_with_repos(["new-repo-global"])
        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.side_effect = RuntimeError(
            "lock service down"
        )

        updater = MetaDirectoryUpdater(
            str(cidx_meta_path), registry, refresh_scheduler=mock_scheduler
        )
        updater.update()

        # File must NOT have been created (update was skipped due to lock failure)
        assert not (cidx_meta_path / "new-repo.md").exists(), (
            "update() created files despite lock acquisition raising an exception"
        )


# ---------------------------------------------------------------------------
# CLASS 11: Division-by-zero guard in MetaDirectoryMassDeleteBlocked (M1)
# ---------------------------------------------------------------------------


class TestMassDeleteBlockedDivisionByZeroGuard:
    """M1: MetaDirectoryMassDeleteBlocked.__init__ must not raise ZeroDivisionError."""

    def test_exception_message_when_existing_count_is_zero(self):
        """
        MetaDirectoryMassDeleteBlocked(to_delete_count=5, existing_count=0, aliases=set())
        must not raise ZeroDivisionError and must produce a readable message.
        """
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryMassDeleteBlocked,
        )

        # This should NOT raise ZeroDivisionError
        exc = MetaDirectoryMassDeleteBlocked(5, 0, {"a-global", "b-global"})
        msg = str(exc)

        # Message must be non-empty and not crash
        assert len(msg) > 0
        # Must contain N/A or safe ratio representation when existing_count=0
        assert "N/A" in msg or "0" in msg
