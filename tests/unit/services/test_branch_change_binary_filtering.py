"""TDD tests for Bug #469: Branch change path indexes binary files.

Root cause: GitTopologyService.analyze_branch_change() returns raw git diff
output without filtering through file_extensions or override config. Binary
files (.jar, .zip, .exe, .psd) that differ between branches get passed
directly to the embedding pipeline.

The fix must filter files_to_reindex through the same extension/override
rules that FileFinder uses for the normal indexing path.
"""

import subprocess
from pathlib import Path


from code_indexer.config import Config
from code_indexer.services.git_topology_service import GitTopologyService


def _create_git_repo(path: Path) -> None:
    """Create a git repo with initial commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("# Test\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )


def _create_branch_with_files(
    repo: Path, branch: str, files: dict, message: str
) -> None:
    """Create a branch and add files to it."""
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", branch],
        check=True,
        capture_output=True,
    )
    for rel_path, content in files.items():
        full = repo / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            full.write_bytes(content)
        else:
            full.write_text(content)
    subprocess.run(
        ["git", "-C", str(repo), "add", "--all"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        check=True,
        capture_output=True,
    )


def _switch_branch(repo: Path, branch: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "checkout", branch],
        check=True,
        capture_output=True,
    )


BINARY_CONTENT = b"PK\x03\x04" + b"\x00" * 200


class TestBranchChangeBinaryFiltering:
    """Prove that analyze_branch_change returns binary files unfiltered."""

    def _setup_repo_with_branches(self, tmp_path: Path) -> Path:
        """Create repo with main branch and feature branch containing mixed files."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        # Add source + binary files on a feature branch
        _create_branch_with_files(
            repo,
            "feature",
            {
                "code/src/Main.java": "public class Main {}\n",
                "code/src/Utils.kt": "fun main() {}\n",
                "code/src/Config.py": "config = {}\n",
                # Binary junk
                "code/3dparty/metro/webservices-rt.jar": BINARY_CONTENT,
                "code/3dparty/zelix/ZKM.jar": BINARY_CONTENT,
                "code/lib/groovy-all-2.4.6.jar": BINARY_CONTENT,
                "code/clientside/app.exe": BINARY_CONTENT,
                "code/clientside/lib.dll": BINARY_CONTENT,
                "code/clientside/VX805_Driver.zip": BINARY_CONTENT,
                "MediaResources/photoshop.old/design.psd": BINARY_CONTENT,
                "MediaResources/photoshop.old/artwork.xcf": BINARY_CONTENT,
                "buildtools/exe4j/tool.exe": BINARY_CONTENT,
                "buildtools/exe4j/icon.png": BINARY_CONTENT,
            },
            "add mixed files",
        )

        # Go back to main so we can analyze the branch change
        _switch_branch(repo, "master")
        return repo

    def test_analyze_branch_change_excludes_jar_files(self, tmp_path: Path) -> None:
        """files_to_reindex must NOT contain .jar files."""
        repo = self._setup_repo_with_branches(tmp_path)
        config = Config(codebase_dir=repo)
        svc = GitTopologyService(repo, config=config)

        analysis = svc.analyze_branch_change("master", "feature")
        jar_files = [f for f in analysis.files_to_reindex if f.endswith(".jar")]

        assert len(jar_files) == 0, (
            f"Bug #469: analyze_branch_change returned {len(jar_files)} .jar files "
            f"in files_to_reindex: {jar_files}. "
            f"Binary files must be filtered out — jar is NOT in file_extensions."
        )

    def test_analyze_branch_change_excludes_zip_files(self, tmp_path: Path) -> None:
        """files_to_reindex must NOT contain .zip files."""
        repo = self._setup_repo_with_branches(tmp_path)
        config = Config(codebase_dir=repo)
        svc = GitTopologyService(repo, config=config)

        analysis = svc.analyze_branch_change("master", "feature")
        zip_files = [f for f in analysis.files_to_reindex if f.endswith(".zip")]

        assert len(zip_files) == 0, (
            f"Bug #469: analyze_branch_change returned {len(zip_files)} .zip files "
            f"in files_to_reindex: {zip_files}"
        )

    def test_analyze_branch_change_excludes_exe_files(self, tmp_path: Path) -> None:
        """files_to_reindex must NOT contain .exe files."""
        repo = self._setup_repo_with_branches(tmp_path)
        config = Config(codebase_dir=repo)
        svc = GitTopologyService(repo, config=config)

        analysis = svc.analyze_branch_change("master", "feature")
        exe_files = [f for f in analysis.files_to_reindex if f.endswith(".exe")]

        assert len(exe_files) == 0, (
            f"Bug #469: analyze_branch_change returned {len(exe_files)} .exe files "
            f"in files_to_reindex: {exe_files}"
        )

    def test_analyze_branch_change_excludes_all_binary_extensions(
        self, tmp_path: Path
    ) -> None:
        """files_to_reindex must not contain ANY binary extension."""
        repo = self._setup_repo_with_branches(tmp_path)
        config = Config(codebase_dir=repo)
        svc = GitTopologyService(repo, config=config)

        analysis = svc.analyze_branch_change("master", "feature")

        binary_exts = {".jar", ".zip", ".exe", ".dll", ".psd", ".xcf", ".png"}
        binary_files = [
            f
            for f in analysis.files_to_reindex
            if Path(f).suffix.lower() in binary_exts
        ]

        assert len(binary_files) == 0, (
            f"Bug #469: analyze_branch_change returned {len(binary_files)} binary files "
            f"in files_to_reindex: {binary_files}"
        )

    def test_analyze_branch_change_includes_source_files(self, tmp_path: Path) -> None:
        """files_to_reindex MUST contain the source files that changed."""
        repo = self._setup_repo_with_branches(tmp_path)
        config = Config(codebase_dir=repo)
        svc = GitTopologyService(repo, config=config)

        analysis = svc.analyze_branch_change("master", "feature")

        source_files = [
            f
            for f in analysis.files_to_reindex
            if Path(f).suffix.lower().lstrip(".") in config.file_extensions
        ]

        # We added 3 source files: Main.java, Utils.kt, Config.py
        assert len(source_files) >= 3, (
            f"Expected at least 3 source files in files_to_reindex, got {len(source_files)}: "
            f"{source_files}. Full list: {analysis.files_to_reindex}"
        )

    def test_analyze_branch_change_only_returns_indexable_files(
        self, tmp_path: Path
    ) -> None:
        """Every file in files_to_reindex must have an extension in file_extensions."""
        repo = self._setup_repo_with_branches(tmp_path)
        config = Config(codebase_dir=repo)
        svc = GitTopologyService(repo, config=config)

        analysis = svc.analyze_branch_change("master", "feature")

        for f in analysis.files_to_reindex:
            ext = Path(f).suffix.lower().lstrip(".")
            assert ext in config.file_extensions, (
                f"Bug #469: '{f}' (extension '{ext}') is in files_to_reindex but "
                f"'{ext}' is NOT in file_extensions. Only indexable source files "
                f"should be returned."
            )

    def test_production_scenario_mixed_repo(self, tmp_path: Path) -> None:
        """Replicate exact production scenario: Java/Kotlin repo with binary junk.

        13 source files changed + hundreds of binary files between branches.
        Only source files should appear in files_to_reindex.
        """
        repo = tmp_path / "evolution"
        repo.mkdir()
        _create_git_repo(repo)

        # Build a feature branch with production-like file mix
        files = {}
        # 13 source files (the real changes)
        for i in range(8):
            files[f"code/src/dms/server/Service{i}.java"] = f"class Service{i} {{}}\n"
        for i in range(5):
            files[f"code/src/dms/server/Module{i}.kt"] = f"class Module{i}\n"

        # Hundreds of binary junk files (should NOT be indexed)
        for i in range(20):
            files[f"code/3dparty/metro/lib{i}.jar"] = BINARY_CONTENT
        for i in range(10):
            files[f"code/lib/vendor{i}.jar"] = BINARY_CONTENT
        for i in range(5):
            files[f"code/clientside/tool{i}.exe"] = BINARY_CONTENT
        for i in range(5):
            files[f"code/clientside/lib{i}.dll"] = BINARY_CONTENT
        for i in range(3):
            files[f"code/clientside/archive{i}.zip"] = BINARY_CONTENT
        for i in range(10):
            files[f"MediaResources/photoshop.old/design{i}.psd"] = BINARY_CONTENT
        for i in range(5):
            files[f"MediaResources/photoshop.old/art{i}.xcf"] = BINARY_CONTENT
        for i in range(5):
            files[f"buildtools/exe4j/tool{i}.exe"] = BINARY_CONTENT

        _create_branch_with_files(repo, "feature", files, "mixed changes")
        _switch_branch(repo, "master")

        config = Config(codebase_dir=repo)
        svc = GitTopologyService(repo, config=config)

        analysis = svc.analyze_branch_change("master", "feature")

        source_count = sum(
            1
            for f in analysis.files_to_reindex
            if Path(f).suffix.lower().lstrip(".") in config.file_extensions
        )
        binary_count = len(analysis.files_to_reindex) - source_count

        assert binary_count == 0, (
            f"Bug #469 production scenario: {binary_count} binary files in files_to_reindex "
            f"(expected 0). Total: {len(analysis.files_to_reindex)}, source: {source_count}. "
            f"Sample binary: {[f for f in analysis.files_to_reindex if Path(f).suffix.lower().lstrip('.') not in config.file_extensions][:5]}"
        )
        assert source_count == 13, f"Expected 13 source files, got {source_count}"
