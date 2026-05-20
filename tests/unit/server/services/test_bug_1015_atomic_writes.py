"""
Tests for Bug #1015: atomic writes using temp-file + rename pattern.

Three write sites in dependency_map_service.py must use:
    tmp_path = target_file.with_suffix(".tmp")
    tmp_path.write_text(content)
    tmp_path.replace(target_file)

instead of the non-atomic:
    target_file.write_text(content)

Tests verify:
1. Path.replace() is called (proves atomic rename is used).
2. No .tmp file remains after a successful write.
3. Final file content matches intended content.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.services.dependency_map_service import DependencyMapService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path) -> DependencyMapService:
    """Return a DependencyMapService with minimal mocked dependencies."""
    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = str(tmp_path / "golden-repos")

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=MagicMock(),
        tracking_backend=MagicMock(),
        analyzer=MagicMock(),
    )


def _make_config(fact_check_enabled: bool = False) -> MagicMock:
    """Return a minimal config mock for _update_domain_file."""
    config = MagicMock()
    config.dep_map_fact_check_enabled = fact_check_enabled
    config.dependency_map_pass_timeout_seconds = 60
    config.dependency_map_delta_max_turns = 3
    return config


# ---------------------------------------------------------------------------
# Site 1: _update_domain_file  — domain .md write
# ---------------------------------------------------------------------------


class TestUpdateDomainFileAtomicWrite:
    """Bug #1015: _update_domain_file must use temp-file + rename."""

    def test_update_domain_file_uses_atomic_write(self, tmp_path: Path) -> None:
        """_update_domain_file must call Path.replace() to atomically rename .tmp."""
        service = _make_service(tmp_path)

        domain_dir = tmp_path / "dep-map"
        domain_dir.mkdir()
        domain_file = domain_dir / "auth.md"
        # Pre-existing content so _update_domain_file can read it
        domain_file.write_text("---\ndomain: auth\n---\n\nExisting body.")

        # Stub the analyzer: invoke_delta_merge_file returns new body content
        new_body = "Updated auth body."
        service._analyzer.invoke_delta_merge_file.return_value = new_body
        service._analyzer.build_delta_merge_prompt.return_value = "some prompt"
        service._activity_journal = MagicMock()
        service._activity_journal.journal_path = tmp_path / "journal.jsonl"

        config = _make_config(fact_check_enabled=False)

        replace_calls: list = []

        # Patch Path.replace to record calls while still executing the real rename
        original_replace = Path.replace

        def tracking_replace(self_path: Path, target: Path) -> Path:
            replace_calls.append((str(self_path), str(target)))
            return original_replace(self_path, target)

        with patch.object(Path, "replace", tracking_replace):
            service._update_domain_file(
                domain_name="auth",
                domain_file=domain_file,
                changed_repos=["repo-a"],
                new_repos=[],
                removed_repos=[],
                domain_list=["auth"],
                config=config,
            )

        # Must have called replace() at least once targeting domain_file
        target_paths = [t for _, t in replace_calls]
        assert str(domain_file) in target_paths, (
            f"Expected Path.replace() targeting {domain_file} but got: {replace_calls}"
        )

    def test_atomic_write_no_tmp_file_left_on_success_update_domain_file(
        self, tmp_path: Path
    ) -> None:
        """After _update_domain_file succeeds, no .tmp file must remain."""
        service = _make_service(tmp_path)

        domain_dir = tmp_path / "dep-map"
        domain_dir.mkdir()
        domain_file = domain_dir / "auth.md"
        domain_file.write_text("---\ndomain: auth\n---\n\nExisting body.")

        service._analyzer.invoke_delta_merge_file.return_value = "New body."
        service._analyzer.build_delta_merge_prompt.return_value = "prompt"
        service._activity_journal = MagicMock()
        service._activity_journal.journal_path = tmp_path / "journal.jsonl"

        config = _make_config(fact_check_enabled=False)

        service._update_domain_file(
            domain_name="auth",
            domain_file=domain_file,
            changed_repos=["repo-a"],
            new_repos=[],
            removed_repos=[],
            domain_list=["auth"],
            config=config,
        )

        tmp_file = domain_file.with_suffix(".tmp")
        assert not tmp_file.exists(), (
            f"Stale .tmp file found after successful write: {tmp_file}"
        )

    def test_atomic_write_content_matches_expected_update_domain_file(
        self, tmp_path: Path
    ) -> None:
        """_update_domain_file must write the intended content to domain_file."""
        service = _make_service(tmp_path)

        domain_dir = tmp_path / "dep-map"
        domain_dir.mkdir()
        domain_file = domain_dir / "auth.md"
        domain_file.write_text(
            "---\ndomain: auth\nlast_analyzed: 2024-01-01\n---\n\nOld body."
        )

        expected_body = "Fresh updated auth content."
        service._analyzer.invoke_delta_merge_file.return_value = expected_body
        service._analyzer.build_delta_merge_prompt.return_value = "prompt"
        service._activity_journal = MagicMock()
        service._activity_journal.journal_path = tmp_path / "journal.jsonl"

        config = _make_config(fact_check_enabled=False)

        service._update_domain_file(
            domain_name="auth",
            domain_file=domain_file,
            changed_repos=["repo-a"],
            new_repos=[],
            removed_repos=[],
            domain_list=["auth"],
            config=config,
        )

        written = domain_file.read_text()
        assert expected_body in written, (
            f"Expected body not found in written content.\n"
            f"Expected to contain: {expected_body!r}\n"
            f"Got: {written!r}"
        )


# ---------------------------------------------------------------------------
# Site 2: _apply_domain_assignments  — _domains.json write
# ---------------------------------------------------------------------------


class TestApplyDomainAssignmentsAtomicWrite:
    """Bug #1015: _apply_domain_assignments must use temp-file + rename."""

    def test_apply_domain_assignments_uses_atomic_write(self, tmp_path: Path) -> None:
        """_apply_domain_assignments must call Path.replace() for _domains.json."""
        service = _make_service(tmp_path)

        dep_map_dir = tmp_path / "dep-map"
        dep_map_dir.mkdir()

        assignments = [{"repo": "repo-a", "domains": ["auth"]}]
        domain_list: list = []

        replace_calls: list = []
        original_replace = Path.replace

        def tracking_replace(self_path: Path, target: Path) -> Path:
            replace_calls.append((str(self_path), str(target)))
            return original_replace(self_path, target)

        with patch.object(Path, "replace", tracking_replace):
            service._apply_domain_assignments(
                assignments=assignments,
                domain_list=domain_list,
                dependency_map_dir=dep_map_dir,
            )

        domains_json = dep_map_dir / "_domains.json"
        target_paths = [t for _, t in replace_calls]
        assert str(domains_json) in target_paths, (
            f"Expected Path.replace() targeting {domains_json} but got: {replace_calls}"
        )

    def test_atomic_write_no_tmp_file_left_on_success_apply_domain_assignments(
        self, tmp_path: Path
    ) -> None:
        """After _apply_domain_assignments succeeds, no .tmp file must remain."""
        service = _make_service(tmp_path)

        dep_map_dir = tmp_path / "dep-map"
        dep_map_dir.mkdir()

        assignments = [{"repo": "repo-b", "domains": ["billing"]}]

        service._apply_domain_assignments(
            assignments=assignments,
            domain_list=[],
            dependency_map_dir=dep_map_dir,
        )

        domains_json = dep_map_dir / "_domains.json"
        tmp_file = domains_json.with_suffix(".tmp")
        assert not tmp_file.exists(), (
            f"Stale .tmp file found after successful write: {tmp_file}"
        )


# ---------------------------------------------------------------------------
# Site 3: _remove_stale_repos_from_domains_json  — _domains.json write
# ---------------------------------------------------------------------------


class TestRemoveStaleReposAtomicWrite:
    """Bug #1015: _remove_stale_repos_from_domains_json must use temp-file + rename."""

    def _setup_versioned_domains(
        self, tmp_path: Path, domain_list: list
    ) -> tuple[Path, Path]:
        """
        Create the versioned cidx-meta layout that _get_cidx_meta_read_path() resolves.

        Returns (golden_repos_dir, live_dep_map_dir).
        """
        golden_repos_dir = tmp_path / "golden-repos"
        versioned_dir = golden_repos_dir / ".versioned" / "cidx-meta" / "v_0001"
        versioned_dep_map = versioned_dir / "dependency-map"
        versioned_dep_map.mkdir(parents=True)
        (versioned_dep_map / "_domains.json").write_text(
            json.dumps(domain_list, indent=2)
        )

        live_dep_map = golden_repos_dir / "cidx-meta" / "dependency-map"
        live_dep_map.mkdir(parents=True)

        return golden_repos_dir, live_dep_map

    def test_remove_stale_repos_uses_atomic_write(self, tmp_path: Path) -> None:
        """_remove_stale_repos_from_domains_json must call Path.replace()."""
        domain_list = [
            {"name": "auth", "participating_repos": ["repo-a", "stale-repo"]},
        ]
        golden_repos_dir, live_dep_map = self._setup_versioned_domains(
            tmp_path, domain_list
        )

        golden_repos_manager = MagicMock()
        golden_repos_manager.golden_repos_dir = str(golden_repos_dir)
        service = DependencyMapService(
            golden_repos_manager=golden_repos_manager,
            config_manager=MagicMock(),
            tracking_backend=MagicMock(),
            analyzer=MagicMock(),
        )

        replace_calls: list = []
        original_replace = Path.replace

        def tracking_replace(self_path: Path, target: Path) -> Path:
            replace_calls.append((str(self_path), str(target)))
            return original_replace(self_path, target)

        with patch.object(Path, "replace", tracking_replace):
            result = service._remove_stale_repos_from_domains_json(
                removed_repos=["stale-repo"],
                dependency_map_dir=live_dep_map,
            )

        assert result is True
        domains_json = live_dep_map / "_domains.json"
        target_paths = [t for _, t in replace_calls]
        assert str(domains_json) in target_paths, (
            f"Expected Path.replace() targeting {domains_json} but got: {replace_calls}"
        )

    def test_atomic_write_no_tmp_file_left_on_success_remove_stale_repos(
        self, tmp_path: Path
    ) -> None:
        """After _remove_stale_repos_from_domains_json succeeds, no .tmp remains."""
        domain_list = [
            {"name": "billing", "participating_repos": ["repo-x", "gone-repo"]},
        ]
        golden_repos_dir, live_dep_map = self._setup_versioned_domains(
            tmp_path, domain_list
        )

        golden_repos_manager = MagicMock()
        golden_repos_manager.golden_repos_dir = str(golden_repos_dir)
        service = DependencyMapService(
            golden_repos_manager=golden_repos_manager,
            config_manager=MagicMock(),
            tracking_backend=MagicMock(),
            analyzer=MagicMock(),
        )

        service._remove_stale_repos_from_domains_json(
            removed_repos=["gone-repo"],
            dependency_map_dir=live_dep_map,
        )

        domains_json = live_dep_map / "_domains.json"
        tmp_file = domains_json.with_suffix(".tmp")
        assert not tmp_file.exists(), (
            f"Stale .tmp file found after successful write: {tmp_file}"
        )
