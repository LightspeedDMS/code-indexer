"""
Unit tests for Bug #396: Stale repo cleanup from _domains.json on golden repo deletion.

Bug #396:
- When a golden repo is deleted, run_delta_analysis() detects it as a removed repo.
- However, the repo alias is never removed from _domains.json participating_repos.
- This causes stale entries to accumulate across delta refreshes.

Fix:
- Add _remove_stale_repos_from_domains_json() method.
- Call it from run_delta_analysis() after _update_affected_domains() completes.
"""

import json
from unittest.mock import Mock, patch

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_service(tmp_path):
    """Build a DependencyMapService for Bug #396 stale repo cleanup testing."""
    gm = Mock()
    gm.golden_repos_dir = str(tmp_path)
    tracking = Mock()
    tracking.get_tracking.return_value = {
        "status": "completed",
        "commit_hashes": json.dumps({}),
        "next_run": None,
    }
    config_mgr = Mock()
    config = Mock()
    config.dependency_map_enabled = True
    config.dependency_map_interval_hours = 24
    config.dependency_map_delta_max_turns = 5
    config.dependency_map_pass_timeout_seconds = 120
    config_mgr.get_claude_integration_config.return_value = config
    analyzer = Mock()
    return DependencyMapService(gm, config_mgr, tracking, analyzer)


def _setup_versioned_domains(tmp_path, domains):
    """Write _domains.json to the versioned cidx-meta read path (mimics real repo layout).

    The read path that _get_cidx_meta_read_path() resolves to for a local cidx-meta
    repo (no .versioned/ dir) is golden_repos_dir/cidx-meta/.
    """
    read_dir = tmp_path / "cidx-meta" / "dependency-map"
    read_dir.mkdir(parents=True, exist_ok=True)
    (read_dir / "_domains.json").write_text(json.dumps(domains))
    return read_dir


class TestRemoveStaleReposFromDomainsJson:
    """_remove_stale_repos_from_domains_json: direct unit tests."""

    def test_method_exists_on_service(self, tmp_path):
        """Bug #396: DependencyMapService must expose _remove_stale_repos_from_domains_json."""
        svc = _make_service(tmp_path)
        assert hasattr(
            svc, "_remove_stale_repos_from_domains_json"
        ), "DependencyMapService must have _remove_stale_repos_from_domains_json method"

    def test_stale_repos_removed_from_domains_json(self, tmp_path):
        """Bug #396: Removed repo alias is stripped from participating_repos in _domains.json."""
        svc = _make_service(tmp_path)

        domains = [
            {
                "name": "backend",
                "description": "Backend services",
                "participating_repos": ["repo-a", "repo-b", "repo-c"],
            }
        ]
        _read_dir = _setup_versioned_domains(tmp_path, domains)

        # WRITE path: live dependency-map dir
        live_dir = tmp_path / "cidx-meta-live" / "dependency-map"
        live_dir.mkdir(parents=True, exist_ok=True)

        result = svc._remove_stale_repos_from_domains_json(
            removed_repos=["repo-b"],
            dependency_map_dir=live_dir,
        )

        assert result is True
        written = json.loads((live_dir / "_domains.json").read_text())
        backend = next((d for d in written if d["name"] == "backend"), None)
        assert backend is not None
        assert (
            "repo-b" not in backend["participating_repos"]
        ), "repo-b must be removed from participating_repos"
        assert "repo-a" in backend["participating_repos"], "repo-a must remain"
        assert "repo-c" in backend["participating_repos"], "repo-c must remain"

    def test_stale_repo_removed_from_multiple_domains(self, tmp_path):
        """Bug #396: Stale repo alias removed from every domain that lists it."""
        svc = _make_service(tmp_path)

        domains = [
            {
                "name": "backend",
                "description": "Backend",
                "participating_repos": ["repo-x", "repo-y"],
            },
            {
                "name": "frontend",
                "description": "Frontend",
                "participating_repos": ["repo-x", "repo-z"],
            },
        ]
        _setup_versioned_domains(tmp_path, domains)

        live_dir = tmp_path / "cidx-meta-live" / "dependency-map"
        live_dir.mkdir(parents=True, exist_ok=True)

        result = svc._remove_stale_repos_from_domains_json(
            removed_repos=["repo-x"],
            dependency_map_dir=live_dir,
        )

        assert result is True
        written = json.loads((live_dir / "_domains.json").read_text())

        backend = next((d for d in written if d["name"] == "backend"), None)
        frontend = next((d for d in written if d["name"] == "frontend"), None)

        assert backend is not None
        assert "repo-x" not in backend["participating_repos"]
        assert "repo-y" in backend["participating_repos"]

        assert frontend is not None
        assert "repo-x" not in frontend["participating_repos"]
        assert "repo-z" in frontend["participating_repos"]

    def test_no_modification_when_no_removed_repos(self, tmp_path):
        """Bug #396: Empty removed_repos list causes no file write and returns True."""
        svc = _make_service(tmp_path)

        domains = [
            {
                "name": "backend",
                "description": "Backend",
                "participating_repos": ["repo-a"],
            }
        ]
        _setup_versioned_domains(tmp_path, domains)

        live_dir = tmp_path / "cidx-meta-live" / "dependency-map"
        live_dir.mkdir(parents=True, exist_ok=True)

        result = svc._remove_stale_repos_from_domains_json(
            removed_repos=[],
            dependency_map_dir=live_dir,
        )

        assert result is True
        # No file should be written for an empty removal list
        assert not (
            live_dir / "_domains.json"
        ).exists(), "_domains.json must NOT be written when removed_repos is empty"

    def test_no_modification_when_repo_not_in_any_domain(self, tmp_path):
        """Bug #396: Repo alias not present in any domain causes no content change."""
        svc = _make_service(tmp_path)

        domains = [
            {
                "name": "backend",
                "description": "Backend",
                "participating_repos": ["repo-a", "repo-b"],
            }
        ]
        _setup_versioned_domains(tmp_path, domains)

        live_dir = tmp_path / "cidx-meta-live" / "dependency-map"
        live_dir.mkdir(parents=True, exist_ok=True)

        result = svc._remove_stale_repos_from_domains_json(
            removed_repos=["nonexistent-repo"],
            dependency_map_dir=live_dir,
        )

        assert result is True
        # No file should be written because nothing changed
        assert not (
            live_dir / "_domains.json"
        ).exists(), "_domains.json must NOT be written when the repo alias is not found in any domain"

    def test_domains_json_not_found_returns_true(self, tmp_path):
        """Bug #396: Missing _domains.json is not an error — returns True (nothing to clean)."""
        svc = _make_service(tmp_path)

        # No _domains.json written — read path has no file
        live_dir = tmp_path / "cidx-meta-live" / "dependency-map"
        live_dir.mkdir(parents=True, exist_ok=True)

        result = svc._remove_stale_repos_from_domains_json(
            removed_repos=["some-deleted-repo"],
            dependency_map_dir=live_dir,
        )

        assert result is True

    def test_empty_domains_after_removal_still_retained(self, tmp_path):
        """Bug #396: Domains with zero participating_repos after removal are NOT pruned."""
        svc = _make_service(tmp_path)

        domains = [
            {
                "name": "solo-domain",
                "description": "Domain with only one repo",
                "participating_repos": ["only-repo"],
            }
        ]
        _setup_versioned_domains(tmp_path, domains)

        live_dir = tmp_path / "cidx-meta-live" / "dependency-map"
        live_dir.mkdir(parents=True, exist_ok=True)

        result = svc._remove_stale_repos_from_domains_json(
            removed_repos=["only-repo"],
            dependency_map_dir=live_dir,
        )

        assert result is True
        written = json.loads((live_dir / "_domains.json").read_text())
        solo = next((d for d in written if d["name"] == "solo-domain"), None)
        assert (
            solo is not None
        ), "Domain with zero repos must NOT be pruned from _domains.json"
        assert (
            solo["participating_repos"] == []
        ), "participating_repos must be empty list"


class TestRunDeltaAnalysisStaleRepoCleanup:
    """Bug #396: run_delta_analysis() calls _remove_stale_repos_from_domains_json for removed repos."""

    def test_cleanup_called_when_removed_repos_detected(self, tmp_path):
        """Bug #396: When detect_changes returns removed_repos, cleanup method is called."""
        svc = _make_service(tmp_path)

        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)
        (depmap_dir / "_index.md").write_text(
            "---\nschema_version: 1.0\n---\n\n"
            "## Repo-to-Domain Matrix\n\n"
            "| Repository | Domain |\n|---|---|\n"
            "| deleted-repo | backend |\n"
        )
        (depmap_dir / "_domains.json").write_text(
            json.dumps(
                [
                    {
                        "name": "backend",
                        "description": "Backend",
                        "participating_repos": ["deleted-repo", "live-repo"],
                    }
                ]
            )
        )

        removed_repo = {
            "alias": "deleted-repo",
            "clone_path": str(tmp_path / "deleted-repo"),
        }
        changed_repo = {"alias": "live-repo", "clone_path": str(tmp_path / "live-repo")}

        with (
            patch.object(
                svc, "detect_changes", return_value=([changed_repo], [], [removed_repo])
            ),
            patch.object(svc, "identify_affected_domains", return_value={"backend"}),
            patch.object(svc, "_update_affected_domains", return_value=[]),
            patch.object(svc, "_finalize_delta_tracking"),
            patch.object(svc, "_get_activated_repos", return_value=[changed_repo]),
            patch.object(
                svc, "_remove_stale_repos_from_domains_json", return_value=True
            ) as mock_cleanup,
        ):
            svc._analyzer.generate_claude_md.return_value = None
            svc.run_delta_analysis()

        mock_cleanup.assert_called_once()
        call_kwargs = mock_cleanup.call_args[1]
        assert "removed_repos" in call_kwargs
        removed_aliases = [r.get("alias") for r in call_kwargs["removed_repos"]]
        assert "deleted-repo" in removed_aliases

    def test_cleanup_called_via_early_return_when_no_affected_domains_but_removed_repos(
        self, tmp_path
    ):
        """Bug #396 (early-return path): _remove_stale_repos_from_domains_json is called even
        when identify_affected_domains returns empty (stale _index.md cannot map removed repo
        to any domain), causing the early return at 'if not affected_domains'.

        Before the fix, the early return at line 2074 would fire and skip the cleanup block
        that appears later in run_delta_analysis(), leaving stale repo aliases in _domains.json.
        """
        svc = _make_service(tmp_path)

        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)
        # _index.md is stale: deleted-repo does NOT appear in the matrix.
        # This causes identify_affected_domains to return empty set even though
        # deleted-repo is in removed_repos.
        (depmap_dir / "_index.md").write_text(
            "---\nschema_version: 1.0\n---\n\n"
            "## Repo-to-Domain Matrix\n\n"
            "| Repository | Domain |\n|---|---|\n"
            # Note: deleted-repo intentionally absent (stale _index.md)
        )
        (depmap_dir / "_domains.json").write_text(
            json.dumps(
                [
                    {
                        "name": "backend",
                        "description": "Backend",
                        "participating_repos": ["deleted-repo", "live-repo"],
                    }
                ]
            )
        )

        removed_repo = {
            "alias": "deleted-repo",
            "clone_path": str(tmp_path / "deleted-repo"),
        }

        with (
            patch.object(svc, "detect_changes", return_value=([], [], [removed_repo])),
            patch.object(
                # Simulate stale _index.md: no domain found for deleted-repo → empty set
                svc,
                "identify_affected_domains",
                return_value=set(),
            ),
            patch.object(svc, "_finalize_delta_tracking"),
            patch.object(svc, "_get_activated_repos", return_value=[]),
            patch.object(
                svc, "_remove_stale_repos_from_domains_json", return_value=True
            ) as mock_cleanup,
        ):
            result = svc.run_delta_analysis()

        assert result == {"status": "completed", "affected_domains": 0}
        mock_cleanup.assert_called_once()
        call_kwargs = mock_cleanup.call_args[1]
        assert "removed_repos" in call_kwargs
        removed_aliases = [r.get("alias") for r in call_kwargs["removed_repos"]]
        assert "deleted-repo" in removed_aliases, (
            "_remove_stale_repos_from_domains_json must be called with deleted-repo "
            "even when identify_affected_domains returns empty set"
        )

    def test_cleanup_not_called_when_no_removed_repos(self, tmp_path):
        """Bug #396: When no repos removed, cleanup method is NOT called."""
        svc = _make_service(tmp_path)

        depmap_dir = tmp_path / "cidx-meta" / "dependency-map"
        depmap_dir.mkdir(parents=True)
        (depmap_dir / "_index.md").write_text(
            "---\nschema_version: 1.0\n---\n\n"
            "## Repo-to-Domain Matrix\n\n"
            "| Repository | Domain |\n|---|---|\n"
        )
        (depmap_dir / "_domains.json").write_text(
            json.dumps(
                [
                    {
                        "name": "backend",
                        "description": "Backend",
                        "participating_repos": ["live-repo"],
                    }
                ]
            )
        )

        changed_repo = {"alias": "live-repo", "clone_path": str(tmp_path / "live-repo")}

        with (
            patch.object(svc, "detect_changes", return_value=([changed_repo], [], [])),
            patch.object(svc, "identify_affected_domains", return_value={"backend"}),
            patch.object(svc, "_update_affected_domains", return_value=[]),
            patch.object(svc, "_finalize_delta_tracking"),
            patch.object(svc, "_get_activated_repos", return_value=[changed_repo]),
            patch.object(
                svc, "_remove_stale_repos_from_domains_json", return_value=True
            ) as mock_cleanup,
        ):
            svc._analyzer.generate_claude_md.return_value = None
            svc.run_delta_analysis()

        mock_cleanup.assert_not_called()
