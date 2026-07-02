"""
Regression test for Story #775: Delete tests/mcpb/ directory and clean fast-automation.sh.

Asserts:
- tests/mcpb/ directory does not exist on disk or in git (tracked files)
- fast-automation.sh contains no reference to tests/mcpb/ or test_bridge_e2e_real
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def _fast_automation_content() -> str:
    """Return the full text of fast-automation.sh from the repository root."""
    return (REPO_ROOT / "fast-automation.sh").read_text()


def test_mcpb_directory_does_not_exist_on_disk() -> None:
    """tests/mcpb/ must be fully removed from the filesystem."""
    mcpb_path = REPO_ROOT / "tests" / "mcpb"
    assert not mcpb_path.exists(), (
        f"tests/mcpb/ still exists at {mcpb_path} — delete it with `git rm -r tests/mcpb/`"
    )


def test_mcpb_directory_not_tracked_in_git() -> None:
    """tests/mcpb/ must not appear in git-tracked files."""
    result = subprocess.run(
        ["git", "ls-files", "tests/mcpb/"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, f"git ls-files failed: {result.stderr}"
    assert result.stdout.strip() == "", (
        f"tests/mcpb/ still has git-tracked files:\n{result.stdout}"
    )


def test_fast_automation_has_no_mcpb_directory_reference() -> None:
    """fast-automation.sh must not reference tests/mcpb/ (pytest invocation line removed)."""
    assert "tests/mcpb/" not in _fast_automation_content(), (
        "fast-automation.sh still contains 'tests/mcpb/' — remove the pytest invocation line"
    )


def test_fast_automation_has_no_test_bridge_e2e_real_reference() -> None:
    """fast-automation.sh must not reference test_bridge_e2e_real (--ignore line removed)."""
    assert "test_bridge_e2e_real" not in _fast_automation_content(), (
        "fast-automation.sh still contains 'test_bridge_e2e_real' — remove the --ignore line"
    )
