"""
Setup script for code-indexer with custom hnswlib build.

This setup.py builds hnswlib from the third_party/hnswlib submodule instead
of using the PyPI package. The custom build includes the check_integrity()
method required for index integrity validation.

Installation:
    1. Initialize submodule: git submodule update --init
    2. Install: pip install -e .
"""

import subprocess
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.develop import develop
from setuptools.command.install import install


class CustomDevelopCommand(develop):
    """Custom development installation that builds hnswlib from submodule."""

    def run(self):
        """Build hnswlib submodule, then run standard develop installation."""
        self._build_hnswlib_submodule()
        develop.run(self)

    def _build_hnswlib_submodule(self):
        """Build and install hnswlib from third_party/hnswlib submodule."""
        project_root = Path(__file__).parent.absolute()
        hnswlib_dir = project_root / "third_party" / "hnswlib"

        if not hnswlib_dir.exists():
            print(
                "ERROR: third_party/hnswlib submodule not found.\n"
                "Run: git submodule update --init",
                file=sys.stderr,
            )
            sys.exit(1)

        if not (hnswlib_dir / ".git").exists():
            print(
                "ERROR: third_party/hnswlib is not initialized as a git submodule.\n"
                "Run: git submodule update --init",
                file=sys.stderr,
            )
            sys.exit(1)

        print("Building hnswlib from third_party/hnswlib submodule...")

        # Build and install hnswlib in development mode
        result = subprocess.run(
            [sys.executable, "setup.py", "develop"],
            cwd=str(hnswlib_dir),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print(f"ERROR: Failed to build hnswlib:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)

        print("Successfully built hnswlib from submodule")


class CustomInstallCommand(install):
    """Custom installation that builds hnswlib from submodule."""

    def run(self):
        """Build hnswlib submodule, then run standard installation."""
        self._build_hnswlib_submodule()
        install.run(self)

    def _build_hnswlib_submodule(self):
        """Build and install hnswlib from third_party/hnswlib submodule."""
        project_root = Path(__file__).parent.absolute()
        hnswlib_dir = project_root / "third_party" / "hnswlib"

        if not hnswlib_dir.exists():
            print(
                "ERROR: third_party/hnswlib submodule not found.\n"
                "Run: git submodule update --init",
                file=sys.stderr,
            )
            sys.exit(1)

        print("Building hnswlib from third_party/hnswlib submodule...")

        # Build and install hnswlib
        result = subprocess.run(
            [sys.executable, "setup.py", "install"],
            cwd=str(hnswlib_dir),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print(f"ERROR: Failed to build hnswlib:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)

        print("Successfully built hnswlib from submodule")


# Use pyproject.toml for main configuration
# This setup.py only handles hnswlib submodule build
setup(
    cmdclass={
        "develop": CustomDevelopCommand,
        "install": CustomInstallCommand,
    },
)
