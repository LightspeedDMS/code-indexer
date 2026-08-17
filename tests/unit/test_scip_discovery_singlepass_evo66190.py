"""EVO-66190: discovery does a SINGLE filesystem walk (not ~13 rglob passes).

These lock in the behavior the single-walk refactor adds/keeps: VCS/index dirs are
pruned (no bogus projects, and the walk doesn't descend a huge .git tree), the walk
collects settings/pom/pattern files correctly, and discover()'s output is unchanged for
the multi-module and mixed cases.
"""

from src.code_indexer.scip.discovery import ProjectDiscovery


class TestPruning:
    def test_build_files_in_git_and_code_indexer_are_ignored(self, tmp_path):
        # A real project at the root...
        (tmp_path / "package.json").write_text("{}")
        # ...and decoy build files inside pruned dirs that must NOT become projects.
        for pruned in (".git", ".code-indexer"):
            d = tmp_path / pruned / "nested"
            d.mkdir(parents=True)
            (d / "package.json").write_text("{}")
            (d / "pom.xml").write_text("<project/>")
        projects = ProjectDiscovery(tmp_path).discover()
        paths = sorted(str(p.relative_path) for p in projects)
        assert paths == ["."]  # only the root project; pruned decoys excluded

    def test_pruned_dir_is_not_descended(self, tmp_path):
        # settings.gradle inside .git must not register a Gradle root.
        g = tmp_path / ".git"
        g.mkdir()
        (g / "settings.gradle").write_text("")
        (tmp_path / "go.mod").write_text("module x")
        projects = ProjectDiscovery(tmp_path).discover()
        assert [str(p.relative_path) for p in projects] == ["."]
        assert projects[0].build_system == "module"  # go, not gradle


class TestWalkCollector:
    def test_walk_collects_settings_pom_and_patterns(self, tmp_path):
        (tmp_path / "settings.gradle").write_text("include 'a'")
        (tmp_path / "pom.xml").write_text("<project/>")
        (tmp_path / "svc").mkdir()
        (tmp_path / "svc" / "App.csproj").write_text("<Project/>")
        (tmp_path / "go.mod").write_text("module x")
        settings, poms, by_pattern = ProjectDiscovery(tmp_path)._walk_build_files()
        assert [p.name for p in settings] == ["settings.gradle"]
        assert [p.name for p in poms] == ["pom.xml"]
        assert [p.name for p in by_pattern["go.mod"]] == ["go.mod"]
        assert [p.name for p in by_pattern["*.csproj"]] == ["App.csproj"]

    def test_missing_repo_root_yields_empty(self, tmp_path):
        # os.walk on a non-existent dir yields nothing (onerror swallowed), no raise.
        missing = tmp_path / "does-not-exist"
        settings, poms, by_pattern = ProjectDiscovery(missing)._walk_build_files()
        assert settings == [] and poms == []
        assert all(v == [] for v in by_pattern.values())
        assert ProjectDiscovery(missing).discover() == []


class TestGradleRootUnchanged:
    def test_gradle_multiproject_still_one_root(self, tmp_path):
        (tmp_path / "settings.gradle").write_text("include 'a', 'b'")
        for sub in ("a", "b"):
            d = tmp_path / sub
            d.mkdir()
            (d / "build.gradle").write_text("")
        projects = ProjectDiscovery(tmp_path).discover()
        assert [str(p.relative_path) for p in projects] == ["."]
        assert projects[0].build_system == "gradle"
