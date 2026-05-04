"""Lazy-load CI gate tests for X-Ray module.

These tests verify that importing the CLI does NOT trigger tree_sitter imports.
This is the critical invariant that keeps cidx --help at ~1.3s.

Run in fast-automation.sh - blocks merge if failing.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent / "src")
PROJECT_ROOT = str(Path(__file__).parent.parent.parent.parent)


class TestLazyLoadInProcess:
    """Verify tree_sitter modules are absent after CLI import.

    In-process checks are unreliable because pytest loads tree_sitter via
    earlier tests in the same session.  These tests use a fresh subprocess so
    the Python interpreter is clean — no contamination from other test files.
    """

    def test_tree_sitter_not_in_modules_after_cli_import(self) -> None:
        """AC: tree_sitter absent from sys.modules after CLI import (subprocess proof)."""
        code = (
            "import sys, json; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.cli import cli; "
            "print('tree_sitter:', 'tree_sitter' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "tree_sitter: False" in result.stdout, (
            f"LAZY-LOAD VIOLATION: tree_sitter was imported at CLI startup.\n"
            f"Subprocess output: {result.stdout!r}"
        )

    def test_tree_sitter_languages_not_in_modules_after_cli_import(self) -> None:
        """AC: tree_sitter_languages absent from sys.modules after CLI import (subprocess proof)."""
        code = (
            "import sys, json; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.cli import cli; "
            "print('tree_sitter_languages:', 'tree_sitter_languages' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "tree_sitter_languages: False" in result.stdout, (
            f"LAZY-LOAD VIOLATION: tree_sitter_languages was imported at CLI startup.\n"
            f"Subprocess output: {result.stdout!r}"
        )


class TestLazyLoadSubprocess:
    """Subprocess-based test: fresh interpreter, cannot be contaminated."""

    def test_tree_sitter_absent_in_fresh_interpreter(self) -> None:
        """Spawn fresh Python, import CLI, assert tree_sitter modules absent."""
        code = (
            "import sys, json; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.cli import cli; "
            "modules = sorted(sys.modules.keys()); "
            "print(json.dumps(modules))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        loaded_modules = json.loads(result.stdout.strip())
        assert "tree_sitter" not in loaded_modules, (
            f"LAZY-LOAD VIOLATION: tree_sitter was imported at CLI startup. "
            f"This regresses cidx --help startup time. "
            f"Found in modules: {[m for m in loaded_modules if 'tree_sitter' in m]}"
        )
        assert "tree_sitter_languages" not in loaded_modules, (
            "LAZY-LOAD VIOLATION: tree_sitter_languages was imported at CLI startup."
        )

    def test_xray_module_absent_in_fresh_interpreter(self) -> None:
        """Verify the xray package itself is not eagerly imported at CLI startup."""
        code = (
            "import sys, json; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.cli import cli; "
            "xray_mods = [m for m in sys.modules if 'xray' in m]; "
            "print(json.dumps(xray_mods))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"
        xray_modules = json.loads(result.stdout.strip())
        # xray __init__.py may be imported (it has no heavy imports), but
        # ast_engine must not be imported (it does the lazy tree_sitter import)
        assert "code_indexer.xray.ast_engine" not in xray_modules, (
            "ast_engine was eagerly imported at CLI startup - this would import tree_sitter"
        )


class TestCidxHelpTiming:
    """cidx --help must complete within 2.0s (current baseline ~1.3s)."""

    def test_cidx_help_under_2s(self) -> None:
        """Wall clock for 'cidx --help' subprocess must be under 2.0 seconds."""
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "code_indexer.cli", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            env={
                **__import__("os").environ,
                "PYTHONPATH": SRC_ROOT,
            },
        )
        elapsed = time.monotonic() - start

        # --help exits with code 0
        assert result.returncode == 0, (
            f"cidx --help failed (rc={result.returncode}):\n{result.stderr}"
        )
        assert elapsed < 2.0, (
            f"cidx --help took {elapsed:.2f}s, exceeding 2.0s budget. "
            f"X-Ray lazy-load may have regressed startup time."
        )
