"""
E2E-style tests for the -global.md filename migration.

Verifies that:
1. _migrate_global_suffix_filenames() renames {alias}-global.md -> {alias}.md
   and preserves frontmatter byte-for-byte.
2. Migration skips rename when {alias}.md already exists.
3. Migration runs BEFORE _find_terse_description_aliases() is called in start().
4. MetaDirectoryUpdater.update() does NOT contain migration logic.
"""

import shutil
import tempfile
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared test content helpers
# ---------------------------------------------------------------------------

FRONTMATTER_TEMPLATE = """\
---
lifecycle:
  branching:
    default_branch: main
    model: github-flow
  build_system: hatchling
  ci:
    deploy_on: manual
    required_checks:
    - build
    - test
  confidence: medium
  language_ecosystem: python/hatch
lifecycle_schema_version: 4
---

{description}
"""


def _make_alias_content(description: str) -> str:
    return FRONTMATTER_TEMPLATE.format(description=description)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cidx_meta_dir():
    """Temporary cidx-meta directory."""
    tmp = tempfile.mkdtemp()
    p = Path(tmp)
    yield p
    shutil.rmtree(tmp, ignore_errors=True)


def _make_minimal_scheduler(
    cidx_meta_dir: Path,
    aliases: List[str],
):
    """Build a DescriptionRefreshScheduler with minimal real wiring.

    Uses injectable backend mode (tracking_backend + golden_backend) so
    db_path is not required. golden_backend is a MagicMock whose
    list_repos() returns the given aliases. This is the seam used for
    observation in the ordering test.
    """
    from code_indexer.server.storage.sqlite_backends import (
        DescriptionRefreshTrackingBackend,
    )
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )

    tmp_db = tempfile.mktemp(suffix=".db")

    tracking_backend = DescriptionRefreshTrackingBackend(tmp_db)
    golden_backend = MagicMock()
    golden_backend.list_repos.return_value = [{"alias": alias} for alias in aliases]

    config_manager = MagicMock()
    config = MagicMock()
    config.claude_integration_config.max_concurrent_claude_cli = 1
    config_manager.load_config.return_value = config

    scheduler = DescriptionRefreshScheduler(
        config_manager=config_manager,
        meta_dir=cidx_meta_dir,
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
    )
    return scheduler, golden_backend


# ---------------------------------------------------------------------------
# Test 1: Migration preserves frontmatter byte-for-byte
# ---------------------------------------------------------------------------


class TestMigrationPreservesFrontmatter:
    """_migrate_global_suffix_filenames() renames files and preserves content exactly."""

    def test_migration_preserves_frontmatter_before_backfill(self, cidx_meta_dir):
        """
        Production scenario recreation:
        - 3 {alias}-global.md files with REAL frontmatter exist
        - No {alias}.md files exist
        - After migration: {alias}.md files exist with identical content
        - {alias}-global.md files are gone

        This is the exact failure mode: MetaDirectoryUpdater.update() was
        renaming AFTER DescriptionRefreshScheduler.start() scanned for terse
        descriptions, causing expensive Claude CLI regeneration to destroy
        existing frontmatter.
        """
        aliases = ["humanize", "JSqlParser", "SpringBoot"]
        descriptions = [
            "humanize is a Python library that converts machine-oriented values into human-friendly text.",
            "JSqlParser is a SQL statement parser for Java that translates SQL statements into a traversable hierarchy of Java classes.",
            "SpringBoot makes it easy to create stand-alone, production-grade Spring based Applications.",
        ]

        # Create -global.md files with real frontmatter (production state)
        original_contents = {}
        for alias, desc in zip(aliases, descriptions):
            content = _make_alias_content(desc)
            file_path = cidx_meta_dir / f"{alias}-global.md"
            file_path.write_text(content, encoding="utf-8")
            original_contents[alias] = content

        scheduler, _ = _make_minimal_scheduler(cidx_meta_dir, aliases=aliases)

        count = scheduler._migrate_global_suffix_filenames()

        assert count == 3, f"Expected 3 files renamed, got {count}"

        for alias in aliases:
            short_file = cidx_meta_dir / f"{alias}.md"
            global_file = cidx_meta_dir / f"{alias}-global.md"

            assert short_file.exists(), f"{alias}.md must exist after migration"
            assert not global_file.exists(), (
                f"{alias}-global.md must be gone after migration"
            )

            actual_content = short_file.read_text(encoding="utf-8")
            assert actual_content == original_contents[alias], (
                f"Content of {alias}.md differs from original — "
                f"frontmatter was not preserved byte-for-byte"
            )


# ---------------------------------------------------------------------------
# Test 2: Migration skips when short alias already exists
# ---------------------------------------------------------------------------


class TestMigrationSkipsWhenShortAliasExists:
    """Migration must not overwrite {alias}.md when it already exists."""

    def test_migration_skips_when_short_alias_exists(self, cidx_meta_dir):
        """
        Both foo-global.md and foo.md exist with different content.
        Migration must skip — neither file modified, count=0.
        """
        global_content = "# foo (global version)\nsome old content\n"
        short_content = "# foo\nsome new content\n"

        (cidx_meta_dir / "foo-global.md").write_text(global_content)
        (cidx_meta_dir / "foo.md").write_text(short_content)

        scheduler, _ = _make_minimal_scheduler(cidx_meta_dir, aliases=["foo"])
        count = scheduler._migrate_global_suffix_filenames()

        assert count == 0, "Migration must not rename when target already exists"
        assert (cidx_meta_dir / "foo-global.md").exists(), (
            "foo-global.md must still exist (skipped)"
        )
        assert (cidx_meta_dir / "foo.md").exists(), (
            "foo.md must still exist (not touched)"
        )
        assert (cidx_meta_dir / "foo.md").read_text() == short_content, (
            "foo.md content must be unchanged"
        )
        assert (cidx_meta_dir / "foo-global.md").read_text() == global_content, (
            "foo-global.md content must be unchanged"
        )


# ---------------------------------------------------------------------------
# Test 3: Migration runs before terse scan
# ---------------------------------------------------------------------------


class TestMigrationRunsBeforeTerseScan:
    """start() must call _migrate_global_suffix_filenames() BEFORE reconcile_terse_descriptions()."""

    def test_migration_runs_before_terse_scan(self, cidx_meta_dir):
        """
        Ordering proof via the injected golden_backend seam (external dependency).

        golden_backend.list_repos() is called multiple times during start():
          Call 1: from _migrate_global_suffix_filenames() — pre-migration state
          Call 2: from the first reconciliation method — post-migration state

        We capture filesystem state on every call. The ordering proof:
          - Call 1 sees {alias}-global.md (migration hasn't renamed yet)
          - Call 2 sees {alias}.md (migration completed before reconciliation)

        lifecycle_invoker is wired (MagicMock) so reconcile_terse_descriptions()
        reaches _list_golden_aliases() instead of short-circuiting.
        """
        aliases = ["humanize", "JSqlParser"]
        for alias in aliases:
            content = _make_alias_content("Short desc.")
            (cidx_meta_dir / f"{alias}-global.md").write_text(content)

        scheduler, golden_backend = _make_minimal_scheduler(
            cidx_meta_dir, aliases=aliases
        )
        scheduler._lifecycle_invoker = MagicMock()
        scheduler._golden_repos_dir = cidx_meta_dir.parent
        scheduler._lifecycle_debouncer = MagicMock()
        scheduler._refresh_scheduler = MagicMock()
        scheduler._job_tracker = MagicMock()

        snapshots: list = []
        repo_rows = [{"alias": alias} for alias in aliases]

        def capturing_list_repos():
            snap = {}
            for alias in aliases:
                snap[f"{alias}.md"] = (cidx_meta_dir / f"{alias}.md").exists()
                snap[f"{alias}-global.md"] = (
                    cidx_meta_dir / f"{alias}-global.md"
                ).exists()
            snapshots.append(snap)
            return repo_rows

        golden_backend.list_repos.side_effect = capturing_list_repos

        scheduler._config_manager.load_config.return_value.claude_integration_config.description_refresh_enabled = True
        scheduler._config_manager.load_config.return_value.claude_integration_config.description_refresh_interval_hours = 24

        with patch("threading.Thread") as mock_thread_class:
            mock_thread_class.return_value = MagicMock()
            scheduler.start()

        assert len(snapshots) >= 2, (
            f"Expected at least 2 list_repos() calls, got {len(snapshots)}"
        )

        # Call 1 (from migration): -global.md exists, .md does not yet
        for alias in aliases:
            assert snapshots[0][f"{alias}-global.md"], (
                f"Call 1: {alias}-global.md should exist (pre-migration)"
            )
            assert not snapshots[0][f"{alias}.md"], (
                f"Call 1: {alias}.md should NOT exist yet (pre-migration)"
            )

        # Call 2 (first reconciliation): .md exists, -global.md gone
        for alias in aliases:
            assert snapshots[1][f"{alias}.md"], (
                f"Call 2: {alias}.md must exist (migration completed before reconciliation)"
            )
            assert not snapshots[1][f"{alias}-global.md"], (
                f"Call 2: {alias}-global.md must be gone (migration completed)"
            )


# ---------------------------------------------------------------------------
# Test 4: MetaDirectoryUpdater no longer has migration
# ---------------------------------------------------------------------------


class TestMetaDirectoryUpdaterNoLongerHasMigration:
    """MetaDirectoryUpdater.update() must NOT rename -global.md files."""

    def test_meta_directory_updater_no_longer_has_migration(self, cidx_meta_dir):
        """
        Create {alias}-global.md files, run update(), assert they are NOT renamed.
        The migration code has been removed from MetaDirectoryUpdater.update().
        If this test fails it means migration logic is still present in the
        MetaDirectoryUpdater class.
        """
        from code_indexer.global_repos.meta_directory_updater import (
            MetaDirectoryUpdater,
        )

        aliases = ["JSqlParser", "SpringBoot"]

        for alias in aliases:
            global_content = f"# {alias}\nSome content.\n"
            (cidx_meta_dir / f"{alias}-global.md").write_text(global_content)

        registry = MagicMock()
        registry.list_global_repos.return_value = [
            {"alias_name": f"{alias}-global"} for alias in aliases
        ]

        updater = MetaDirectoryUpdater(str(cidx_meta_dir), registry)
        updater.update()

        for alias in aliases:
            global_file = cidx_meta_dir / f"{alias}-global.md"
            assert global_file.exists(), (
                f"{alias}-global.md was renamed by MetaDirectoryUpdater.update() "
                f"— migration must have been removed from this class"
            )
