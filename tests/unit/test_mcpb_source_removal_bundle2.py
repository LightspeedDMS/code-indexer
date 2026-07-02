"""
Regression test for Bundle 2 of Epic #756 MCPB source removal.

Stories covered:
  #831 - Resolve scripts/build_binary.py MCPB coupling (delete script + companion test)
  #826 - Delete src/code_indexer/mcpb/ module
  #827 - Remove MCPB entry points from pyproject.toml

All assertions are real filesystem / git / file-content checks.
No mocks.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_not_tracked(rel_path: str) -> None:
    """Assert that rel_path has no git-tracked files under REPO_ROOT."""
    result = subprocess.run(
        ["git", "ls-files", rel_path],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"git ls-files failed for {rel_path}: {result.stderr}"
    )
    assert result.stdout.strip() == "", (
        f"{rel_path} is still tracked by git:\n{result.stdout}"
    )


def _pyproject_text() -> str:
    """Return the full text of pyproject.toml."""
    return (REPO_ROOT / "pyproject.toml").read_text()


# ---------------------------------------------------------------------------
# Story #831 — scripts/build_binary.py and its companion test must be deleted
# ---------------------------------------------------------------------------


def test_build_binary_script_does_not_exist() -> None:
    """scripts/build_binary.py must not exist on disk."""
    target = REPO_ROOT / "scripts" / "build_binary.py"
    assert not target.exists(), (
        f"scripts/build_binary.py still exists at {target} — "
        "delete it with `git rm scripts/build_binary.py`"
    )


def test_build_binary_test_does_not_exist() -> None:
    """tests/unit/scripts/test_build_binary.py must not exist on disk."""
    target = REPO_ROOT / "tests" / "unit" / "scripts" / "test_build_binary.py"
    assert not target.exists(), (
        f"tests/unit/scripts/test_build_binary.py still exists at {target} — "
        "delete it with `git rm tests/unit/scripts/test_build_binary.py`"
    )


def test_build_binary_script_not_tracked_in_git() -> None:
    """scripts/build_binary.py must not appear in git-tracked files."""
    _assert_not_tracked("scripts/build_binary.py")


def test_build_binary_test_not_tracked_in_git() -> None:
    """tests/unit/scripts/test_build_binary.py must not appear in git-tracked files."""
    _assert_not_tracked("tests/unit/scripts/test_build_binary.py")


# ---------------------------------------------------------------------------
# Story #826 — src/code_indexer/mcpb/ directory must be deleted entirely
# ---------------------------------------------------------------------------


def test_mcpb_module_directory_does_not_exist() -> None:
    """src/code_indexer/mcpb/ must not exist on disk."""
    target = REPO_ROOT / "src" / "code_indexer" / "mcpb"
    assert not target.exists(), (
        f"src/code_indexer/mcpb/ still exists at {target} — "
        "delete it with `git rm -r src/code_indexer/mcpb/`"
    )


def test_mcpb_module_not_tracked_in_git() -> None:
    """src/code_indexer/mcpb/ must have zero git-tracked files."""
    _assert_not_tracked("src/code_indexer/mcpb/")


# ---------------------------------------------------------------------------
# Story #827 — pyproject.toml must have no MCPB entry points
# ---------------------------------------------------------------------------


def test_pyproject_has_no_cidx_bridge_entry_point() -> None:
    """pyproject.toml must not contain the cidx-bridge entry point."""
    assert "cidx-bridge" not in _pyproject_text(), (
        "pyproject.toml still contains 'cidx-bridge' — "
        'remove the line: cidx-bridge = "code_indexer.mcpb.bridge:main"'
    )


def test_pyproject_has_no_cidx_token_refresh_entry_point() -> None:
    """pyproject.toml must not contain the cidx-token-refresh entry point."""
    assert "cidx-token-refresh" not in _pyproject_text(), (
        "pyproject.toml still contains 'cidx-token-refresh' — "
        'remove the line: cidx-token-refresh = "code_indexer.mcpb.token_refresh:main"'
    )


# ---------------------------------------------------------------------------
# Cross-cutting — no orphan imports of code_indexer.mcpb in src/tests/scripts
# ---------------------------------------------------------------------------


def test_no_orphan_imports_of_code_indexer_mcpb() -> None:
    """No file in src/, tests/, or scripts/ may import code_indexer.mcpb."""
    result = subprocess.run(
        [
            "grep",
            "-rn",
            "code_indexer.mcpb",
            "src/",
            "tests/",
            "scripts/",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    # grep returns 0 = matches found, 1 = no matches, 2 = execution error
    assert result.returncode in (0, 1), (
        f"grep exited with unexpected code {result.returncode}: {result.stderr}"
    )
    # Filter out __pycache__ artefacts and known-safe regression test files
    # that only mention the path as a quoted string inside assertions.
    relevant_lines = [
        line
        for line in result.stdout.splitlines()
        if "__pycache__" not in line
        and "test_mcpb_source_removal_bundle2.py" not in line
        and "test_mcpb_removal_775.py" not in line
    ]
    assert relevant_lines == [], (
        "Orphan imports of code_indexer.mcpb found — all must be removed:\n"
        + "\n".join(relevant_lines)
    )
