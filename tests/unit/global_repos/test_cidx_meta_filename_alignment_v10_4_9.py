"""
Regression tests for v10.4.9: cidx-meta description filename alignment.

Before v10.4.9, on_repo_added() and on_repo_removed() wrote/looked for
{repo_name}.md (e.g. JSqlParser.md) while MetaDirectoryUpdater.update()
expected {alias_name}.md (e.g. JSqlParser-global.md).  Every cidx-meta
refresh cycle therefore treated all hook-created files as orphaned and
deleted them, replacing with 3-line stubs.

Production evidence: cidx-meta commit 971850c deleted 892 files and added
893 stubs in one run (git diff e0be4bd 971850c --stat: 1787 files changed,
2682 insertions, 27622 deletions).

These tests prove that all code paths agree on {alias_name}.md.
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_hook_module_state():
    """Reset module-level singletons before/after each test to avoid cross-test pollution."""
    import code_indexer.global_repos.meta_description_hook as hook_module

    original_refresh_scheduler = getattr(hook_module, "_refresh_scheduler", None)
    original_tracking_backend = getattr(hook_module, "_tracking_backend", None)
    original_scheduler = getattr(hook_module, "_scheduler", None)
    original_debouncer = getattr(hook_module, "_debouncer", None)

    hook_module._refresh_scheduler = None
    hook_module._tracking_backend = None
    hook_module._scheduler = None
    hook_module._debouncer = None

    yield

    hook_module._refresh_scheduler = original_refresh_scheduler
    hook_module._tracking_backend = original_tracking_backend
    hook_module._scheduler = original_scheduler
    hook_module._debouncer = original_debouncer


@pytest.fixture
def golden_repos_dir():
    """Temporary golden-repos directory with cidx-meta subdir."""
    tmp = tempfile.mkdtemp()
    cidx_meta = Path(tmp) / "cidx-meta"
    cidx_meta.mkdir(parents=True)
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def cidx_meta_path(golden_repos_dir):
    """Path to cidx-meta inside the temp golden-repos dir."""
    return golden_repos_dir / "cidx-meta"


class TestOnRepoAddedCreatesAliasFormFilename:
    """
    AC1: on_repo_added() via the Claude CLI path must write {repo_name}-global.md,
    NOT {repo_name}.md.
    """

    def test_on_repo_added_creates_alias_form_filename(
        self, golden_repos_dir, cidx_meta_path
    ):
        """
        Invoke on_repo_added with repo_name='MyRepo' using the Claude CLI path
        (cli_manager.check_cli_available() == True, _generate_repo_description
        mocked to return content).  Assert MyRepo-global.md exists and MyRepo.md
        does NOT — proving the alias-form convention is in effect.
        """
        from code_indexer.global_repos.meta_description_hook import on_repo_added

        repo_name = "MyRepo"
        clone_path = golden_repos_dir / repo_name
        clone_path.mkdir(parents=True)
        (clone_path / "README.md").write_text("# MyRepo\nA test repository.")

        mock_cli_manager = MagicMock()
        mock_cli_manager.check_cli_available.return_value = True

        fake_md_content = (
            "---\nname: MyRepo\n---\n\n# MyRepo\n\nGenerated description.\n"
        )

        mock_ci_config = MagicMock()
        mock_ci_config.claude_integration_config = None  # skip verification pass

        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value = mock_ci_config

        with (
            patch(
                "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
                return_value=mock_cli_manager,
            ),
            patch(
                "code_indexer.global_repos.meta_description_hook._generate_repo_description",
                return_value=fake_md_content,
            ),
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=mock_config_service,
            ),
        ):
            on_repo_added(
                repo_name=repo_name,
                repo_url="https://github.com/example/MyRepo",
                clone_path=str(clone_path),
                golden_repos_dir=str(golden_repos_dir),
            )

        alias_form_file = cidx_meta_path / f"{repo_name}-global.md"
        bare_name_file = cidx_meta_path / f"{repo_name}.md"

        assert alias_form_file.exists(), (
            f"REGRESSION: on_repo_added() did not create alias-form file "
            f"{alias_form_file}. MetaDirectoryUpdater will treat it as missing "
            f"and create a stub, wiping the real description."
        )
        assert not bare_name_file.exists(), (
            f"REGRESSION: hook wrote bare-name file {bare_name_file}; "
            f"MetaDirectoryUpdater will treat it as orphaned and delete it."
        )
        assert "Generated description." in alias_form_file.read_text()


class TestOnRepoRemovedDeletesAliasFormFilename:
    """
    AC2: on_repo_removed() must look for and delete {repo_name}-global.md,
    NOT {repo_name}.md.
    """

    def test_on_repo_removed_deletes_alias_form_filename(
        self, golden_repos_dir, cidx_meta_path
    ):
        """
        Create MyRepo-global.md (what on_repo_added now produces), call
        on_repo_removed('MyRepo', ...), assert MyRepo-global.md is deleted.

        Also create MyRepo.md (the old bug artifact) and assert it is NOT
        deleted — proving on_repo_removed targets the right filename.
        """
        from code_indexer.global_repos.meta_description_hook import on_repo_removed

        repo_name = "MyRepo"
        alias_file = cidx_meta_path / f"{repo_name}-global.md"
        bare_file = cidx_meta_path / f"{repo_name}.md"

        alias_file.write_text("# MyRepo-global\n\nFull description.")
        bare_file.write_text(
            "# MyRepo\n\nOld bare-name artifact (should not be touched)."
        )

        on_repo_removed(
            repo_name=repo_name,
            golden_repos_dir=str(golden_repos_dir),
        )

        assert not alias_file.exists(), (
            f"REGRESSION: on_repo_removed() did not delete {alias_file}."
        )
        # The bare-name file is not managed by this hook — it should remain untouched
        assert bare_file.exists(), (
            f"on_repo_removed() deleted {bare_file} which it should not manage."
        )


class TestMetaDirectoryUpdaterPreservesHookFiles:
    """
    AC3: MetaDirectoryUpdater.update() must NOT delete files created by on_repo_added().
    """

    def test_meta_directory_updater_does_not_delete_hook_files(self, cidx_meta_path):
        """
        Set up cidx-meta with MyRepo-global.md (what on_repo_added now produces).
        Configure registry mock returning alias_name='MyRepo-global'.
        Run MetaDirectoryUpdater.update().  Assert the file is preserved.
        """
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        hook_file = cidx_meta_path / "MyRepo-global.md"
        hook_file.write_text("# MyRepo-global\n\nFull description from Claude CLI.\n")

        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [
            {"alias_name": "MyRepo-global"},
        ]

        updater = MetaDirectoryUpdater(
            meta_dir=str(cidx_meta_path), registry=mock_registry
        )
        updater.update()

        assert hook_file.exists(), (
            "REGRESSION: MetaDirectoryUpdater.update() deleted MyRepo-global.md "
            "— this is the 892-file wipe bug (v10.4.9)."
        )
        content = hook_file.read_text()
        assert "Full description from Claude CLI." in content, (
            "REGRESSION: file was replaced with stub content."
        )


class TestMetaDirectoryUpdaterCreatesMissingFilesInAliasForm:
    """
    AC4: When a registered repo has no description file, MetaDirectoryUpdater
    creates {alias_name}.md with stub content (not {repo_name}.md).
    """

    def test_meta_directory_updater_creates_missing_files_in_alias_form(
        self, cidx_meta_path
    ):
        """
        Registry returns [alias_name='NewRepo-global'], no existing files.
        After update(), NewRepo-global.md must exist; NewRepo.md must NOT.
        """
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [
            {"alias_name": "NewRepo-global"},
        ]

        updater = MetaDirectoryUpdater(
            meta_dir=str(cidx_meta_path), registry=mock_registry
        )
        updater.update()

        expected = cidx_meta_path / "NewRepo-global.md"
        assert expected.exists(), (
            f"MetaDirectoryUpdater.update() did not create {expected}"
        )
        bare = cidx_meta_path / "NewRepo.md"
        assert not bare.exists(), (
            f"MetaDirectoryUpdater created bare-name file {bare} — must use alias form."
        )


class TestRefreshSchedulerReconciliationUsesAliasFormPath:
    """
    AC5: _queue_missing_description() must check {alias_name}.md, not {repo_name}.md.
    """

    def test_refresh_scheduler_reconciliation_uses_alias_form_path(
        self, golden_repos_dir, cidx_meta_path
    ):
        """
        Pre-create MyRepo-global.md and call _queue_missing_description with
        alias_name='MyRepo-global'.  Assert submit_work is NOT called (file
        already present).

        If the function still strips '-global' and checks MyRepo.md, it would
        queue generation (the bug), because MyRepo.md does not exist.
        """
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        existing_desc = cidx_meta_path / "MyRepo-global.md"
        existing_desc.write_text("# MyRepo-global\n\nExisting full description.\n")

        # Instantiate without triggering heavy __init__
        scheduler = object.__new__(RefreshScheduler)
        scheduler.golden_repos_dir = golden_repos_dir

        mock_claude_manager = MagicMock()
        mock_claude_manager.submit_work = MagicMock()

        master_path = golden_repos_dir / "MyRepo"
        master_path.mkdir(parents=True, exist_ok=True)

        result = scheduler._queue_missing_description(
            alias_name="MyRepo-global",
            master_path=master_path,
            claude_cli_manager=mock_claude_manager,
        )

        assert result is False, (
            "REGRESSION: _queue_missing_description returned True (queued) even "
            "though MyRepo-global.md already exists. This means it is still "
            "looking for MyRepo.md (the bare-name bug)."
        )
        mock_claude_manager.submit_work.assert_not_called()


class TestFilenameConventionConsistencyAcrossModules:
    """
    AC6: Anti-regression — on_repo_added() and MetaDirectoryUpdater.update()
    must converge on the SAME file on disk for the same alias.
    """

    def test_filename_convention_consistency_across_modules(
        self, golden_repos_dir, cidx_meta_path
    ):
        """
        Call on_repo_added() for 'SomeProject', then run MetaDirectoryUpdater.update()
        with registry returning alias_name='SomeProject-global'.
        Assert the file created by on_repo_added is NOT deleted and NOT replaced
        with a stub — proving the two modules agree on the filename.
        """
        from code_indexer.global_repos.meta_description_hook import on_repo_added
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        repo_name = "SomeProject"
        clone_path = golden_repos_dir / repo_name
        clone_path.mkdir(parents=True)

        mock_cli_manager = MagicMock()
        mock_cli_manager.check_cli_available.return_value = True

        full_description = "Full generated description for SomeProject."
        fake_md_content = (
            f"---\nname: {repo_name}\n---\n\n# {repo_name}\n\n{full_description}\n"
        )

        mock_ci_config = MagicMock()
        mock_ci_config.claude_integration_config = None

        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value = mock_ci_config

        with (
            patch(
                "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
                return_value=mock_cli_manager,
            ),
            patch(
                "code_indexer.global_repos.meta_description_hook._generate_repo_description",
                return_value=fake_md_content,
            ),
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=mock_config_service,
            ),
        ):
            on_repo_added(
                repo_name=repo_name,
                repo_url="https://github.com/example/SomeProject",
                clone_path=str(clone_path),
                golden_repos_dir=str(golden_repos_dir),
            )

        # Now run MetaDirectoryUpdater — same alias
        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [
            {"alias_name": f"{repo_name}-global"},
        ]
        updater = MetaDirectoryUpdater(
            meta_dir=str(cidx_meta_path), registry=mock_registry
        )
        updater.update()

        alias_file = cidx_meta_path / f"{repo_name}-global.md"
        assert alias_file.exists(), (
            "REGRESSION: MetaDirectoryUpdater deleted the file created by "
            "on_repo_added — the two modules do not agree on the filename."
        )
        content = alias_file.read_text()
        assert full_description in content, (
            "REGRESSION: MetaDirectoryUpdater replaced the hook-written file "
            "with a stub — the two modules do not agree on the filename."
        )
