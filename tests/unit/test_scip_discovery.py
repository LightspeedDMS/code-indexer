"""Unit tests for SCIP project discovery."""

from pathlib import Path
from code_indexer.scip.discovery import ProjectDiscovery


class TestProjectDiscovery:
    """Test project auto-discovery functionality."""

    def test_discover_projects_in_simple_repo(self, tmp_path):
        """Test discovering projects with different build systems in a simple repo."""
        # Arrange: Create a repository with multiple projects
        (tmp_path / "backend" / "pom.xml").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "backend" / "pom.xml").write_text("<project></project>")

        (tmp_path / "frontend" / "package.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        (tmp_path / "frontend" / "package.json").write_text('{"name": "frontend"}')

        (tmp_path / "python-lib" / "pyproject.toml").parent.mkdir(
            parents=True, exist_ok=True
        )
        (tmp_path / "python-lib" / "pyproject.toml").write_text(
            '[tool.poetry]\nname = "python-lib"'
        )

        # Act: Discover projects
        discovery = ProjectDiscovery(tmp_path)
        projects = discovery.discover()

        # Assert: All projects discovered with correct metadata
        assert len(projects) == 3

        # Check backend project
        backend = next(p for p in projects if p.relative_path == Path("backend"))
        assert backend.language == "java"
        assert backend.build_system == "maven"
        assert backend.build_file == Path("backend/pom.xml")

        # Check frontend project
        frontend = next(p for p in projects if p.relative_path == Path("frontend"))
        assert frontend.language == "typescript"
        assert frontend.build_system == "npm"
        assert frontend.build_file == Path("frontend/package.json")

        # Check python-lib project
        python_lib = next(p for p in projects if p.relative_path == Path("python-lib"))
        assert python_lib.language == "python"
        assert python_lib.build_system == "poetry"
        assert python_lib.build_file == Path("python-lib/pyproject.toml")

    def test_discover_nested_projects(self, tmp_path):
        """Test discovering nested projects (monorepo scenario)."""
        # Arrange: Create nested project structure
        (tmp_path / "services" / "api" / "pom.xml").parent.mkdir(
            parents=True, exist_ok=True
        )
        (tmp_path / "services" / "api" / "pom.xml").write_text("<project></project>")

        (tmp_path / "services" / "worker" / "pom.xml").parent.mkdir(
            parents=True, exist_ok=True
        )
        (tmp_path / "services" / "worker" / "pom.xml").write_text("<project></project>")

        (tmp_path / "frontend" / "package.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        (tmp_path / "frontend" / "package.json").write_text('{"name": "frontend"}')

        # Act: Discover projects
        discovery = ProjectDiscovery(tmp_path)
        projects = discovery.discover()

        # Assert: All nested projects discovered
        assert len(projects) == 3
        project_paths = {p.relative_path for p in projects}
        assert Path("services/api") in project_paths
        assert Path("services/worker") in project_paths
        assert Path("frontend") in project_paths

    def test_discover_gradle_projects(self, tmp_path):
        """Test discovering Gradle-based Java/Kotlin projects."""
        # Arrange: Create Gradle projects
        (tmp_path / "java-service" / "build.gradle").parent.mkdir(
            parents=True, exist_ok=True
        )
        (tmp_path / "java-service" / "build.gradle").write_text("// Gradle build")

        (tmp_path / "kotlin-service" / "build.gradle.kts").parent.mkdir(
            parents=True, exist_ok=True
        )
        (tmp_path / "kotlin-service" / "build.gradle.kts").write_text(
            "// Kotlin DSL Gradle"
        )

        # Act: Discover projects
        discovery = ProjectDiscovery(tmp_path)
        projects = discovery.discover()

        # Assert: Gradle projects discovered with correct language
        assert len(projects) == 2

        java_service = next(
            p for p in projects if p.relative_path == Path("java-service")
        )
        assert java_service.language == "java"
        assert java_service.build_system == "gradle"

        kotlin_service = next(
            p for p in projects if p.relative_path == Path("kotlin-service")
        )
        assert kotlin_service.language == "kotlin"
        assert kotlin_service.build_system == "gradle"

    def test_discover_python_setuptools(self, tmp_path):
        """Test discovering Python projects with setup.py."""
        # Arrange: Create setup.py project
        (tmp_path / "python-pkg" / "setup.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "python-pkg" / "setup.py").write_text(
            "from setuptools import setup"
        )

        # Act: Discover projects
        discovery = ProjectDiscovery(tmp_path)
        projects = discovery.discover()

        # Assert: setuptools project discovered
        assert len(projects) == 1
        python_pkg = projects[0]
        assert python_pkg.language == "python"
        assert python_pkg.build_system == "setuptools"
        assert python_pkg.relative_path == Path("python-pkg")

    def test_discover_python_requirements_txt(self, tmp_path):
        """Test discovering Python projects with only requirements.txt."""
        # Arrange: Create Python web app with requirements.txt (no setup.py/pyproject.toml)
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir(parents=True, exist_ok=True)
        (backend_dir / "requirements.txt").write_text("flask==2.0.1\nfastapi==0.68.0")
        (backend_dir / "main.py").write_text("print('hello')")

        # Act: Discover projects
        discovery = ProjectDiscovery(tmp_path)
        projects = discovery.discover()

        # Assert: requirements.txt project discovered
        assert len(projects) == 1
        backend = projects[0]
        assert backend.language == "python"
        assert backend.build_system == "pip"
        assert backend.relative_path == Path("backend")
        assert backend.build_file == Path("backend/requirements.txt")

    def test_discover_empty_repo(self, tmp_path):
        """Test discovering projects in empty repository."""
        # Arrange: Empty directory

        # Act: Discover projects
        discovery = ProjectDiscovery(tmp_path)
        projects = discovery.discover()

        # Assert: No projects found
        assert len(projects) == 0

    def test_discover_python_with_multiple_build_files(self, tmp_path):
        """Test discovering Python project with multiple build files uses priority."""
        # Arrange: Create Python project with all three build files
        python_dir = tmp_path / "python-pkg"
        python_dir.mkdir(parents=True, exist_ok=True)
        (python_dir / "pyproject.toml").write_text('[tool.poetry]\nname = "python-pkg"')
        (python_dir / "setup.py").write_text("from setuptools import setup")
        (python_dir / "requirements.txt").write_text("flask==2.0.1")

        # Act: Discover projects
        discovery = ProjectDiscovery(tmp_path)
        projects = discovery.discover()

        # Assert: Only ONE project discovered using highest priority build file
        assert len(projects) == 1
        python_pkg = projects[0]
        assert python_pkg.language == "python"
        assert python_pkg.build_system == "poetry"
        assert python_pkg.build_file == Path("python-pkg/pyproject.toml")

    def test_discover_csharp_solution(self, tmp_path):
        """Test discovering C# solution projects."""
        # Arrange: Create C# solution
        (tmp_path / "dotnet-app" / "MyApp.sln").parent.mkdir(
            parents=True, exist_ok=True
        )
        (tmp_path / "dotnet-app" / "MyApp.sln").write_text(
            "Microsoft Visual Studio Solution File"
        )

        # Act: Discover projects
        discovery = ProjectDiscovery(tmp_path)
        projects = discovery.discover()

        # Assert: C# solution discovered
        assert len(projects) == 1
        dotnet_app = projects[0]
        assert dotnet_app.language == "csharp"
        assert dotnet_app.build_system == "solution"
        assert dotnet_app.relative_path == Path("dotnet-app")
        assert dotnet_app.build_file == Path("dotnet-app/MyApp.sln")

    def test_discover_csharp_project(self, tmp_path):
        """Test discovering C# project files (.csproj)."""
        # Arrange: Create C# project
        (tmp_path / "dotnet-lib" / "MyLib.csproj").parent.mkdir(
            parents=True, exist_ok=True
        )
        (tmp_path / "dotnet-lib" / "MyLib.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"></Project>'
        )

        # Act: Discover projects
        discovery = ProjectDiscovery(tmp_path)
        projects = discovery.discover()

        # Assert: C# project discovered
        assert len(projects) == 1
        dotnet_lib = projects[0]
        assert dotnet_lib.language == "csharp"
        assert dotnet_lib.build_system == "project"
        assert dotnet_lib.relative_path == Path("dotnet-lib")
        assert dotnet_lib.build_file == Path("dotnet-lib/MyLib.csproj")

    def test_csharp_solution_priority_over_project(self, tmp_path):
        """Test C# solution takes priority over .csproj in same directory."""
        # Arrange: Create directory with both .sln and .csproj
        dotnet_dir = tmp_path / "dotnet-app"
        dotnet_dir.mkdir(parents=True, exist_ok=True)
        (dotnet_dir / "MyApp.sln").write_text("Microsoft Visual Studio Solution File")
        (dotnet_dir / "MyApp.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"></Project>'
        )

        # Act: Discover projects
        discovery = ProjectDiscovery(tmp_path)
        projects = discovery.discover()

        # Assert: Only ONE project discovered using solution (higher priority)
        assert len(projects) == 1
        dotnet_app = projects[0]
        assert dotnet_app.language == "csharp"
        assert dotnet_app.build_system == "solution"
        assert dotnet_app.build_file == Path("dotnet-app/MyApp.sln")

    def test_discover_go_module(self, tmp_path):
        """Test discovering Go module projects."""
        # Arrange: Create Go module project
        (tmp_path / "go-service" / "go.mod").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "go-service" / "go.mod").write_text(
            "module github.com/example/service\n\ngo 1.21"
        )

        # Act: Discover projects
        discovery = ProjectDiscovery(tmp_path)
        projects = discovery.discover()

        # Assert: Go module discovered
        assert len(projects) == 1
        go_service = projects[0]
        assert go_service.language == "go"
        assert go_service.build_system == "module"
        assert go_service.relative_path == Path("go-service")
        assert go_service.build_file == Path("go-service/go.mod")

    # ------------------------------------------------------------------ #
    # EVO-66190: multi-module build roots (Gradle settings / Maven <modules>)
    # ------------------------------------------------------------------ #

    def test_gradle_multiproject_builds_once_at_root(self, tmp_path):
        """A Gradle multi-project (root settings.gradle, no root build.gradle, N
        subproject build.gradle) is a single build -> exactly one project at the root,
        subprojects suppressed. This is the evolution monolith shape."""
        (tmp_path / "settings.gradle").write_text("include 'code:a', 'code:b'")
        (tmp_path / "code" / "a").mkdir(parents=True)
        (tmp_path / "code" / "a" / "build.gradle").write_text("// module a")
        (tmp_path / "code" / "b").mkdir(parents=True)
        (tmp_path / "code" / "b" / "build.gradle").write_text("// module b")

        projects = ProjectDiscovery(tmp_path).discover()

        assert len(projects) == 1
        root = projects[0]
        assert root.relative_path == Path(".")
        assert root.language == "java"
        assert root.build_system == "gradle"
        assert root.build_file == Path("settings.gradle")

    def test_gradle_root_not_overwritten_by_colocated_build_file(self, tmp_path):
        """A Gradle multi-project root that also has a co-located non-JVM build file at
        the root (e.g. a root package.json for lint/tooling) must NOT be silently
        replaced by that file in the per-directory dedup — the root stays the Gradle
        build. (Regression: settings.gradle now carries a real BUILD_FILE_PRIORITY.)"""
        (tmp_path / "settings.gradle").write_text("include 'code:a'")
        (tmp_path / "code" / "a").mkdir(parents=True)
        (tmp_path / "code" / "a" / "build.gradle").write_text("// module a")
        (tmp_path / "package.json").write_text('{"name": "root-tooling"}')

        projects = ProjectDiscovery(tmp_path).discover()

        assert len(projects) == 1
        assert projects[0].relative_path == Path(".")
        assert projects[0].build_system == "gradle"
        assert projects[0].build_file == Path("settings.gradle")

    def test_single_gradle_project_with_settings_not_double_counted(self, tmp_path):
        """A single Gradle project that has both settings.gradle and build.gradle in the
        same dir yields exactly one project (the root), not two."""
        (tmp_path / "settings.gradle").write_text("rootProject.name = 'svc'")
        (tmp_path / "build.gradle").write_text("// root build")

        projects = ProjectDiscovery(tmp_path).discover()

        assert len(projects) == 1
        assert projects[0].relative_path == Path(".")
        assert projects[0].build_system == "gradle"

    def test_maven_aggregator_builds_once_at_root(self, tmp_path):
        """A Maven aggregator (root pom with <modules>) builds its child modules as one
        reactor -> one project at the root, child module poms suppressed."""
        (tmp_path / "pom.xml").write_text(
            '<project xmlns="http://maven.apache.org/POM/4.0.0">'
            "<modules><module>svc-a</module><module>svc-b</module></modules></project>"
        )
        (tmp_path / "svc-a").mkdir()
        (tmp_path / "svc-a" / "pom.xml").write_text("<project><artifactId>a</artifactId></project>")
        (tmp_path / "svc-b").mkdir()
        (tmp_path / "svc-b" / "pom.xml").write_text("<project><artifactId>b</artifactId></project>")

        projects = ProjectDiscovery(tmp_path).discover()

        assert len(projects) == 1
        assert projects[0].relative_path == Path(".")
        assert projects[0].language == "java"
        assert projects[0].build_system == "maven"

    def test_nested_non_jvm_project_inside_gradle_monorepo_is_kept(self, tmp_path):
        """A nested TS project (package.json) inside a Gradle monorepo stays its own
        project — only JVM subprojects are folded into the root build."""
        (tmp_path / "settings.gradle").write_text("include 'code:a'")
        (tmp_path / "code" / "a").mkdir(parents=True)
        (tmp_path / "code" / "a" / "build.gradle").write_text("// module a")
        (tmp_path / "webapp").mkdir()
        (tmp_path / "webapp" / "package.json").write_text('{"name": "webapp"}')

        projects = ProjectDiscovery(tmp_path).discover()

        paths = {p.relative_path for p in projects}
        assert paths == {Path("."), Path("webapp")}
        webapp = next(p for p in projects if p.relative_path == Path("webapp"))
        assert webapp.language == "typescript"
        assert webapp.build_system == "npm"

    def test_standalone_maven_pom_without_modules_unchanged(self, tmp_path):
        """A plain single-module pom (no <modules>) is NOT treated as an aggregator — it
        stays a standalone project at its own dir (regression guard; the phoenix shape)."""
        (tmp_path / "pom.xml").write_text(
            '<project><artifactId>svc</artifactId></project>'
        )

        projects = ProjectDiscovery(tmp_path).discover()

        assert len(projects) == 1
        assert projects[0].relative_path == Path(".")
        assert projects[0].build_system == "maven"
