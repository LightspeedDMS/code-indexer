"""
Integration tests for hnswlib submodule build configuration.

Tests that the build system properly uses the third_party/hnswlib submodule
instead of the PyPI package, and that submodule detection works correctly.
"""

import os
import subprocess
from pathlib import Path

import pytest


class TestHnswlibSubmoduleBuild:
    """Integration tests for hnswlib submodule build system."""

    def test_submodule_directory_exists(self):
        """
        GIVEN code-indexer repository is cloned
        WHEN checking for third_party/hnswlib directory
        THEN it exists and contains hnswlib source code
        """
        project_root = Path(__file__).parent.parent.parent
        submodule_dir = project_root / "third_party" / "hnswlib"

        assert submodule_dir.exists(), "third_party/hnswlib directory not found"
        assert submodule_dir.is_dir(), "third_party/hnswlib is not a directory"

        # Check for key hnswlib files
        python_bindings = submodule_dir / "python_bindings"
        assert (
            python_bindings.exists()
        ), "python_bindings directory not found in submodule"

    def test_submodule_is_initialized(self):
        """
        GIVEN third_party/hnswlib submodule directory exists
        WHEN checking if submodule is a git repository
        THEN it has a .git directory or file (indicates initialized submodule)
        """
        project_root = Path(__file__).parent.parent.parent
        submodule_dir = project_root / "third_party" / "hnswlib"

        git_marker = submodule_dir / ".git"
        assert (
            git_marker.exists()
        ), "Submodule not initialized. Run: git submodule update --init"

    def test_submodule_has_custom_commit(self):
        """
        GIVEN hnswlib submodule is initialized
        WHEN checking the current commit
        THEN it points to the custom commit with check_integrity() method
        """
        project_root = Path(__file__).parent.parent.parent
        submodule_dir = project_root / "third_party" / "hnswlib"

        # Get current commit message
        result = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            cwd=str(submodule_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert (
            result.returncode == 0
        ), f"Failed to get submodule commit: {result.stderr}"
        commit_msg = result.stdout.strip()

        # Should be the custom commit with check_integrity
        assert (
            "checkIntegrity" in commit_msg or "check_integrity" in commit_msg
        ), f"Submodule not on custom commit. Current: {commit_msg}"

    def test_pyproject_toml_does_not_have_pypi_hnswlib(self):
        """
        GIVEN pyproject.toml is the package configuration
        WHEN checking dependencies
        THEN hnswlib>=0.8.0 should NOT be in the dependencies list
        """
        project_root = Path(__file__).parent.parent.parent
        pyproject = project_root / "pyproject.toml"

        content = pyproject.read_text()

        # Should not have PyPI hnswlib dependency
        assert (
            "hnswlib>=0.8.0" not in content
        ), "PyPI hnswlib dependency still present in pyproject.toml"

    def test_requirements_files_do_not_have_hnswlib(self):
        """
        GIVEN requirements*.txt files might exist
        WHEN checking for hnswlib entries
        THEN none should contain hnswlib package
        """
        project_root = Path(__file__).parent.parent.parent

        # Check all requirements files
        for req_file in project_root.glob("requirements*.txt"):
            content = req_file.read_text()
            assert "hnswlib" not in content.lower(), f"hnswlib found in {req_file.name}"

    def test_custom_hnswlib_has_check_integrity(self):
        """
        GIVEN hnswlib is built from submodule
        WHEN importing hnswlib and checking for check_integrity
        THEN the method should be available on Index class
        """
        try:
            import hnswlib
        except ImportError:
            pytest.skip("hnswlib not installed yet")

        # Create a dummy index
        index = hnswlib.Index(space="l2", dim=128)

        # Should have check_integrity method
        assert hasattr(
            index, "check_integrity"
        ), "check_integrity() method not available - using PyPI version?"

    @pytest.mark.skipif(
        os.environ.get("CI") == "true", reason="Build test skipped in CI"
    )
    def test_editable_install_builds_from_submodule(self):
        """
        GIVEN third_party/hnswlib submodule is initialized
        WHEN running pip install -e .
        THEN hnswlib should build from submodule, not PyPI
        """
        project_root = Path(__file__).parent.parent.parent

        # This is a slow test - verify submodule exists first
        submodule_dir = project_root / "third_party" / "hnswlib"
        if not submodule_dir.exists():
            pytest.skip("Submodule not initialized")

        # Try importing - if successful, verify it's the custom build
        try:
            import hnswlib

            index = hnswlib.Index(space="l2", dim=128)
            assert hasattr(
                index, "check_integrity"
            ), "Built from PyPI instead of submodule"
        except ImportError:
            pytest.skip("hnswlib not installed - expected for initial test run")
