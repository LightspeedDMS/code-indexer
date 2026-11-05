"""EVO-66190: SCIP presence (`cidx_scip.server.reconcile.scip_present`, the
golden repo manager's `_scip_present` before modular phase 4d) is
discovery-shape-aware.

A repo with a ROOT build file is indexed as a single root project whose build lands
the TOP-LEVEL `scip/index.scip.db`, so presence requires that top-level DB — a stale
per-subproject nested layout (from the old per-subproject discovery) must NOT count as
"present" (that left JVM monoliths like evolution stuck with 1 indexed module forever).
Repos with no root build file are nested-by-design, so any `.scip.db` still counts.
"""

import os
from pathlib import Path
from unittest import mock

from cidx_scip.server.reconcile import reconcile_scip_from_filesystem, scip_present
from cidx_scip.server.repo_reads import repo_has_root_build_file
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


def _mgr(tmp_path):
    m = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    gr = tmp_path / "golden"
    gr.mkdir(exist_ok=True)
    m.golden_repos_dir = str(gr)
    return m, gr


def _scip_dir(repo: Path) -> Path:
    d = repo / ".code-indexer" / "scip"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestHasRootBuildFile:
    def test_gradle_settings_root(self, tmp_path):
        (tmp_path / "settings.gradle").write_text("include 'a'")
        assert repo_has_root_build_file(tmp_path) is True

    def test_various_root_build_files(self, tmp_path):
        for name in (
            "pom.xml",
            "build.gradle",
            "package.json",
            "pyproject.toml",
            "go.mod",
        ):
            d = tmp_path / name.replace(".", "_")
            d.mkdir()
            (d / name).write_text("x")
            assert repo_has_root_build_file(d) is True, name

    def test_csproj_glob_root(self, tmp_path):
        (tmp_path / "App.csproj").write_text("<Project/>")
        assert repo_has_root_build_file(tmp_path) is True

    def test_no_build_file_or_only_nested(self, tmp_path):
        assert repo_has_root_build_file(tmp_path) is False
        # a build file in a SUBdir is not a root build file
        sub = tmp_path / "service"
        sub.mkdir()
        (sub / "build.gradle").write_text("x")
        assert repo_has_root_build_file(tmp_path) is False


class TestScipPresentShape:
    def test_root_build_stale_nested_only_is_NOT_present(self, tmp_path):
        """The evolution case: root settings.gradle, but only stale nested DBs."""
        m, gr = _mgr(tmp_path)
        repo = gr / "monolith"
        repo.mkdir()
        (repo / "settings.gradle").write_text("include 'code'")
        nested = _scip_dir(repo) / "code" / "util"
        nested.mkdir(parents=True)
        (nested / "index.scip.db").write_bytes(b"x")  # one module built; no root DB
        assert scip_present(repo) is False

    def test_root_build_with_top_level_db_IS_present(self, tmp_path):
        m, gr = _mgr(tmp_path)
        repo = gr / "monolith"
        repo.mkdir()
        (repo / "settings.gradle").write_text("include 'code'")
        (_scip_dir(repo) / "index.scip.db").write_bytes(b"x")  # root build landed
        assert scip_present(repo) is True

    def test_no_root_build_nested_db_still_present(self, tmp_path):
        """Nested-by-design repo (no root build file) — any .scip.db counts (unchanged)."""
        m, gr = _mgr(tmp_path)
        repo = gr / "multi"
        repo.mkdir()
        (repo / "svcA").mkdir()
        (repo / "svcA" / "build.gradle").write_text("x")  # build file in a SUBdir only
        nested = _scip_dir(repo) / "svcA"
        nested.mkdir()
        (nested / "index.scip.db").write_bytes(b"x")
        assert scip_present(repo) is True

    def test_no_scip_dir_is_not_present(self, tmp_path):
        m, gr = _mgr(tmp_path)
        repo = gr / "fresh"
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]")
        assert scip_present(repo) is False


class TestReconcileRebuildsStaleRootRepo:
    def _patches(self, discover_result):
        disc = mock.MagicMock()
        disc.return_value.discover.return_value = discover_result
        funnel = mock.MagicMock(return_value=type("O", (), {"value": "enqueued"})())
        return (
            mock.patch("cidx_scip.discovery.ProjectDiscovery", disc),
            mock.patch("cidx_scip.server.lifecycle.funnel_golden_scip", funnel),
            funnel,
        )

    def test_stale_nested_root_repo_is_reenqueued_not_skipped(self, tmp_path):
        """Before EVO-66190 this repo counted scip_present and was skipped forever."""
        m, gr = _mgr(tmp_path)
        repo = gr / "monolith"
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]")  # root build file
        nested = repo / ".code-indexer" / "scip" / "sub"
        nested.mkdir(parents=True)
        (nested / "index.scip.db").write_bytes(b"x")  # stale nested only, no root DB
        proj = type("P", (), {"language": "python"})()
        p_disc, p_funnel, funnel = self._patches([proj])
        with (
            mock.patch.dict(os.environ, {"CIDX_SCIP_AUTO_RECONCILE_ENABLED": "true"}),
            p_disc,
            p_funnel,
        ):
            counts = reconcile_scip_from_filesystem(m, object())
        funnel.assert_called_once()
        assert counts["enqueued"] == 1
        assert counts["scip_present"] == 0
