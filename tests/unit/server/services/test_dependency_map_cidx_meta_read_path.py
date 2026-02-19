"""
Unit tests for cidx-meta versioned read path fix (Bug: Domain Explorer stale path).

Since Story #224 made cidx-meta a versioned golden repo, reads must come from
the versioned path (.versioned/cidx-meta/v_*/) while writes go to the live path.

Tests:
- test_get_cidx_meta_read_path_returns_versioned_when_available
- test_get_cidx_meta_read_path_falls_back_to_live_when_no_versioned_dir
- test_get_cidx_meta_read_path_falls_back_to_live_on_exception
- test_cidx_meta_read_path_property_delegates_to_method
- test_depmap_dir_uses_versioned_path_when_available
- test_get_activated_repos_reads_description_from_versioned_path
- test_identify_affected_domains_reads_index_from_versioned_path
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_service(golden_repos_dir: str, get_actual_repo_path_side_effect=None):
    """Build a DependencyMapService with a real golden_repos_manager mock."""
    from code_indexer.server.services.dependency_map_service import DependencyMapService

    manager = Mock()
    manager.golden_repos_dir = golden_repos_dir

    if get_actual_repo_path_side_effect is not None:
        manager.get_actual_repo_path.side_effect = get_actual_repo_path_side_effect
    else:
        manager.get_actual_repo_path.return_value = golden_repos_dir + "/cidx-meta"

    service = DependencyMapService(
        golden_repos_manager=manager,
        config_manager=Mock(),
        tracking_backend=Mock(),
        analyzer=Mock(),
    )
    return service


def _make_domain_service(dep_map_svc):
    """Build a DependencyMapDomainService from a DependencyMapService."""
    from code_indexer.server.services.dependency_map_domain_service import (
        DependencyMapDomainService,
    )

    return DependencyMapDomainService(dep_map_svc, Mock())


# ─────────────────────────────────────────────────────────────────────────────
# DependencyMapService._get_cidx_meta_read_path()
# ─────────────────────────────────────────────────────────────────────────────


class TestGetCidxMetaReadPath:
    """_get_cidx_meta_read_path() returns versioned path or falls back to live."""

    def test_returns_versioned_path_when_available(self):
        """
        When get_actual_repo_path('cidx-meta') returns a versioned path,
        _get_cidx_meta_read_path() must return that versioned path.
        """
        with tempfile.TemporaryDirectory() as tmp:
            versioned_path = os.path.join(tmp, ".versioned", "cidx-meta", "v_1700000000")
            os.makedirs(versioned_path)

            service = _make_service(
                tmp,
                get_actual_repo_path_side_effect=lambda alias: versioned_path
                if alias == "cidx-meta"
                else None,
            )

            result = service._get_cidx_meta_read_path()

            assert result == Path(versioned_path)

    def test_falls_back_to_live_path_when_get_actual_repo_path_returns_none(self):
        """
        When get_actual_repo_path('cidx-meta') returns None,
        _get_cidx_meta_read_path() must fall back to the live path.
        """
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(
                tmp,
                get_actual_repo_path_side_effect=lambda alias: None,
            )

            result = service._get_cidx_meta_read_path()

            assert result == Path(tmp) / "cidx-meta"

    def test_falls_back_to_live_path_on_exception(self):
        """
        When get_actual_repo_path('cidx-meta') raises any exception,
        _get_cidx_meta_read_path() must fall back to the live path without propagating.
        """
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(
                tmp,
                get_actual_repo_path_side_effect=RuntimeError("repo not found"),
            )

            result = service._get_cidx_meta_read_path()

            assert result == Path(tmp) / "cidx-meta"

    def test_calls_get_actual_repo_path_with_cidx_meta_alias(self):
        """
        _get_cidx_meta_read_path() must call get_actual_repo_path with 'cidx-meta'.
        """
        with tempfile.TemporaryDirectory() as tmp:
            versioned_path = os.path.join(tmp, ".versioned", "cidx-meta", "v_1700000000")
            os.makedirs(versioned_path)

            service = _make_service(tmp)
            service._golden_repos_manager.get_actual_repo_path.return_value = versioned_path

            service._get_cidx_meta_read_path()

            service._golden_repos_manager.get_actual_repo_path.assert_called_once_with(
                "cidx-meta"
            )


# ─────────────────────────────────────────────────────────────────────────────
# DependencyMapService.cidx_meta_read_path property
# ─────────────────────────────────────────────────────────────────────────────


class TestCidxMetaReadPathProperty:
    """cidx_meta_read_path property delegates to _get_cidx_meta_read_path()."""

    def test_property_delegates_to_method(self):
        """
        cidx_meta_read_path property must return same value as _get_cidx_meta_read_path().
        """
        with tempfile.TemporaryDirectory() as tmp:
            versioned_path = os.path.join(tmp, ".versioned", "cidx-meta", "v_1700000000")
            os.makedirs(versioned_path)

            service = _make_service(tmp)
            service._golden_repos_manager.get_actual_repo_path.return_value = versioned_path

            assert service.cidx_meta_read_path == service._get_cidx_meta_read_path()


# ─────────────────────────────────────────────────────────────────────────────
# DependencyMapDomainService._get_depmap_dir()
# ─────────────────────────────────────────────────────────────────────────────


class TestDomainServiceGetDepMapDir:
    """DependencyMapDomainService._get_depmap_dir() uses versioned cidx-meta path."""

    def test_returns_versioned_dependency_map_dir_when_available(self):
        """
        When cidx_meta_read_path resolves to a versioned path,
        _get_depmap_dir() must return versioned_path / 'dependency-map'.
        """
        with tempfile.TemporaryDirectory() as tmp:
            versioned_path = os.path.join(tmp, ".versioned", "cidx-meta", "v_1700000000")
            os.makedirs(versioned_path)

            service = _make_service(tmp)
            service._golden_repos_manager.get_actual_repo_path.return_value = versioned_path

            domain_svc = _make_domain_service(service)
            result = domain_svc._get_depmap_dir()

            assert result == Path(versioned_path) / "dependency-map"

    def test_returns_live_dependency_map_dir_as_fallback(self):
        """
        When get_actual_repo_path raises an exception (no versioned path),
        _get_depmap_dir() must fall back to live cidx-meta/dependency-map.
        """
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(
                tmp,
                get_actual_repo_path_side_effect=RuntimeError("not found"),
            )

            domain_svc = _make_domain_service(service)
            result = domain_svc._get_depmap_dir()

            assert result == Path(tmp) / "cidx-meta" / "dependency-map"

    def test_get_domain_list_reads_from_versioned_path(self):
        """
        get_domain_list() must read _domains.json from the versioned path.
        Domains written to live path must NOT appear; domains in versioned path MUST appear.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # Create versioned path with _domains.json
            versioned_depmap = Path(tmp) / ".versioned" / "cidx-meta" / "v_1700000000" / "dependency-map"
            versioned_depmap.mkdir(parents=True)
            domains = [{"name": "backend", "repos": [], "description": "Backend services"}]
            (versioned_depmap / "_domains.json").write_text(json.dumps(domains))

            # Create live path with different (stale) _domains.json
            live_depmap = Path(tmp) / "cidx-meta" / "dependency-map"
            live_depmap.mkdir(parents=True)
            stale_domains = [{"name": "stale-domain", "repos": [], "description": "Stale"}]
            (live_depmap / "_domains.json").write_text(json.dumps(stale_domains))

            versioned_path = str(Path(tmp) / ".versioned" / "cidx-meta" / "v_1700000000")
            service = _make_service(tmp)
            service._golden_repos_manager.get_actual_repo_path.return_value = versioned_path

            domain_svc = _make_domain_service(service)
            result = domain_svc.get_domain_list()

            # Must read from versioned path (backend), NOT from stale live path (stale-domain)
            domain_names = [d["name"] for d in result["domains"]]
            assert "backend" in domain_names, (
                "get_domain_list() must read from versioned cidx-meta path"
            )
            assert "stale-domain" not in domain_names, (
                "get_domain_list() must NOT read from stale live path"
            )


# ─────────────────────────────────────────────────────────────────────────────
# DependencyMapService read paths use versioned cidx-meta
# ─────────────────────────────────────────────────────────────────────────────


class TestGetActivatedReposReadsFromVersionedPath:
    """_get_activated_repos() reads {alias}.md from versioned cidx-meta."""

    def test_reads_description_from_versioned_path(self):
        """
        When cidx-meta is versioned, repo description .md files must be read
        from the versioned path, not the stale live path.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # Create versioned path with a repo description file
            versioned_cidx_meta = (
                Path(tmp) / ".versioned" / "cidx-meta" / "v_1700000000"
            )
            versioned_cidx_meta.mkdir(parents=True)
            (versioned_cidx_meta / "my-repo.md").write_text(
                "Versioned description for my-repo"
            )

            # Create stale live path with wrong description
            live_cidx_meta = Path(tmp) / "cidx-meta"
            live_cidx_meta.mkdir(parents=True)
            (live_cidx_meta / "my-repo.md").write_text("Stale description from live path")

            versioned_path = str(versioned_cidx_meta)

            service = _make_service(tmp)
            # get_actual_repo_path called for both "cidx-meta" and "my-repo"
            def side_effect(alias):
                if alias == "cidx-meta":
                    return versioned_path
                elif alias == "my-repo":
                    return str(Path(tmp) / "my-repo")
                raise RuntimeError(f"Unknown alias: {alias}")

            service._golden_repos_manager.get_actual_repo_path.side_effect = side_effect

            # Simulate one activated repo
            service._golden_repos_manager.list_golden_repos.return_value = [
                {"alias": "my-repo", "clone_path": str(Path(tmp) / "my-repo")},
            ]

            result = service._get_activated_repos()

            # Description should come from versioned path
            assert len(result) == 1
            description = result[0]["description_summary"]
            assert description == "Versioned description for my-repo", (
                f"Expected versioned description, got: {description}"
            )


class TestIdentifyAffectedDomainsReadsFromVersionedPath:
    """identify_affected_domains() reads _index.md from versioned cidx-meta."""

    def test_reads_index_md_from_versioned_path(self):
        """
        identify_affected_domains() must read _index.md from the versioned
        cidx-meta path, not from the stale live path.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # Create versioned path with _index.md containing real domain mappings
            versioned_depmap = (
                Path(tmp) / ".versioned" / "cidx-meta" / "v_1700000000" / "dependency-map"
            )
            versioned_depmap.mkdir(parents=True)
            versioned_index = """## Repo-to-Domain Matrix

| Repository | Domains |
|------------|---------|
| my-repo | backend |
"""
            (versioned_depmap / "_index.md").write_text(versioned_index)

            # Create stale live path with different (empty) _index.md
            live_depmap = Path(tmp) / "cidx-meta" / "dependency-map"
            live_depmap.mkdir(parents=True)
            (live_depmap / "_index.md").write_text("## Repo-to-Domain Matrix\n\n")

            versioned_path = str(Path(tmp) / ".versioned" / "cidx-meta" / "v_1700000000")

            service = _make_service(tmp)
            service._golden_repos_manager.get_actual_repo_path.return_value = versioned_path

            # Changed repos: my-repo changed
            changed_repos = [{"alias": "my-repo"}]
            new_repos = []
            removed_repos = []

            affected = service.identify_affected_domains(changed_repos, new_repos, removed_repos)

            # Must find "backend" domain from versioned _index.md
            assert "backend" in affected, (
                f"Expected 'backend' domain from versioned _index.md, got: {affected}"
            )
