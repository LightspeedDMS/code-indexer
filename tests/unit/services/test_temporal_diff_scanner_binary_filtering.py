"""TDD tests for Bug #469: Temporal diff scanner indexes binary files.

Root cause: TemporalDiffScanner._should_include_file() sets base_result=True
for ALL files and only relies on override_filter_service for filtering.
It never checks file_extensions. This means .jar, .zip, .exe files in
git commits get embedded by the temporal indexer.

The fix must check file_extensions BEFORE falling through to base_result=True.
"""

import subprocess
from pathlib import Path

import pytest

from code_indexer.config import Config
from code_indexer.services.temporal.temporal_diff_scanner import TemporalDiffScanner


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


BINARY_CONTENT = b"PK\x03\x04" + b"\x00" * 200


class TestTemporalDiffScannerBinaryFiltering:
    """Prove _should_include_file accepts binary files without extension check."""

    def _make_scanner(self, tmp_path: Path) -> tuple:
        """Create a TemporalDiffScanner with config that has file_extensions."""
        config = Config(codebase_dir=tmp_path)
        scanner = TemporalDiffScanner(
            tmp_path,
            file_extensions=config.file_extensions,
        )
        return scanner, config

    # -----------------------------------------------------------------
    # Binary files MUST be rejected
    # -----------------------------------------------------------------

    def test_should_include_rejects_jar(self, tmp_path: Path) -> None:
        """_should_include_file must return False for .jar files."""
        scanner, config = self._make_scanner(tmp_path)
        assert "jar" not in config.file_extensions
        result = scanner._should_include_file("code/3dparty/metro/webservices-rt.jar")
        assert result is False, (
            "Bug #469: temporal _should_include_file returned True for .jar — "
            "file_extensions not checked, base_result=True lets everything through"
        )

    def test_should_include_rejects_zip(self, tmp_path: Path) -> None:
        """_should_include_file must return False for .zip files."""
        scanner, _ = self._make_scanner(tmp_path)
        result = scanner._should_include_file("code/clientside/VX805_Driver.zip")
        assert result is False, "temporal _should_include_file must reject .zip"

    def test_should_include_rejects_exe(self, tmp_path: Path) -> None:
        """_should_include_file must return False for .exe files."""
        scanner, _ = self._make_scanner(tmp_path)
        result = scanner._should_include_file("buildtools/tool.exe")
        assert result is False, "temporal _should_include_file must reject .exe"

    def test_should_include_rejects_dll(self, tmp_path: Path) -> None:
        """_should_include_file must return False for .dll files."""
        scanner, _ = self._make_scanner(tmp_path)
        result = scanner._should_include_file("code/clientside/lib.dll")
        assert result is False, "temporal _should_include_file must reject .dll"

    def test_should_include_rejects_psd(self, tmp_path: Path) -> None:
        """_should_include_file must return False for .psd files."""
        scanner, _ = self._make_scanner(tmp_path)
        result = scanner._should_include_file("MediaResources/design.psd")
        assert result is False, "temporal _should_include_file must reject .psd"

    @pytest.mark.parametrize(
        "ext",
        [
            "jar",
            "zip",
            "exe",
            "dll",
            "psd",
            "xcf",
            "png",
            "jpg",
            "gif",
            "tif",
            "pdf",
            "gz",
            "dylib",
            "bin",
            "blend",
            "blend1",
            "ttf",
            "ico",
            "sfx",
            "war",
            "so",
            "class",
            "bmp",
            "dic",
            "db",
            "ser",
            "keystore",
        ],
    )
    def test_should_include_rejects_all_production_binary_extensions(
        self, ext: str, tmp_path: Path
    ) -> None:
        """All production binary extensions must be rejected."""
        scanner, config = self._make_scanner(tmp_path)
        assert (
            ext not in config.file_extensions
        ), f"{ext} should not be in file_extensions"
        result = scanner._should_include_file(f"lib/file.{ext}")
        assert result is False, (
            f"temporal _should_include_file must reject .{ext} — "
            f"not in file_extensions"
        )

    # -----------------------------------------------------------------
    # Source files MUST be accepted
    # -----------------------------------------------------------------

    def test_should_include_accepts_java(self, tmp_path: Path) -> None:
        """_should_include_file must return True for .java files."""
        scanner, config = self._make_scanner(tmp_path)
        assert "java" in config.file_extensions
        result = scanner._should_include_file("code/src/Main.java")
        assert result is True, "temporal _should_include_file must accept .java"

    def test_should_include_accepts_kotlin(self, tmp_path: Path) -> None:
        """_should_include_file must return True for .kt files."""
        scanner, _ = self._make_scanner(tmp_path)
        result = scanner._should_include_file("code/src/Utils.kt")
        assert result is True, "temporal _should_include_file must accept .kt"

    def test_should_include_accepts_python(self, tmp_path: Path) -> None:
        """_should_include_file must return True for .py files."""
        scanner, _ = self._make_scanner(tmp_path)
        result = scanner._should_include_file("src/main.py")
        assert result is True, "temporal _should_include_file must accept .py"

    @pytest.mark.parametrize(
        "ext",
        ["java", "kt", "py", "js", "ts", "go", "rs", "cs", "cpp"],
    )
    def test_should_include_accepts_all_source_extensions(
        self, ext: str, tmp_path: Path
    ) -> None:
        """All common source extensions must be accepted."""
        scanner, config = self._make_scanner(tmp_path)
        assert ext in config.file_extensions
        result = scanner._should_include_file(f"src/file.{ext}")
        assert result is True, f"temporal _should_include_file must accept .{ext}"

    # -----------------------------------------------------------------
    # E2E: get_diffs_for_commit must filter binary files
    # -----------------------------------------------------------------

    def test_get_diffs_excludes_binary_files_from_commit(self, tmp_path: Path) -> None:
        """get_diffs_for_commit must not return diffs for binary files."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        # Add mixed files in a commit
        (repo / "code" / "src").mkdir(parents=True)
        (repo / "code" / "src" / "Main.java").write_text("class Main {}\n")
        (repo / "code" / "src" / "Utils.kt").write_text("fun main() {}\n")
        (repo / "code" / "lib").mkdir(parents=True)
        (repo / "code" / "lib" / "vendor.jar").write_bytes(BINARY_CONTENT)
        (repo / "code" / "lib" / "archive.zip").write_bytes(BINARY_CONTENT)
        (repo / "buildtools").mkdir(parents=True)
        (repo / "buildtools" / "tool.exe").write_bytes(BINARY_CONTENT)

        subprocess.run(
            ["git", "-C", str(repo), "add", "--all"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "mixed files"],
            check=True,
            capture_output=True,
        )

        # Get the commit hash
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        commit_hash = result.stdout.strip()

        # Create scanner with file_extensions
        config = Config(codebase_dir=repo)
        scanner = TemporalDiffScanner(
            repo,
            file_extensions=config.file_extensions,
        )

        diffs = scanner.get_diffs_for_commit(commit_hash)
        diff_paths = [d.file_path for d in diffs]

        # Source files must be present
        source_diffs = [p for p in diff_paths if p.endswith((".java", ".kt"))]
        assert (
            len(source_diffs) >= 2
        ), f"Expected at least 2 source file diffs, got {len(source_diffs)}: {source_diffs}"

        # Binary files must NOT be present
        binary_diffs = [p for p in diff_paths if p.endswith((".jar", ".zip", ".exe"))]
        assert len(binary_diffs) == 0, (
            f"Bug #469: get_diffs_for_commit returned {len(binary_diffs)} binary file diffs: "
            f"{binary_diffs}. Binary files must be filtered out."
        )
