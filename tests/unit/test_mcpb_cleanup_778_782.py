"""
Regression tests for Story #778 and Story #782: Remove stale tests/installer/ directory
and clean residual MCPB references from automation scripts.

Asserts:
- tests/installer/ directory does not exist on disk
- tests/installer/ has no git-tracked files
- fast-automation.sh, server-fast-automation.sh, full-automation.sh contain no
  mcpb, cidx-bridge, or cidx-token-refresh references (case-insensitive)
- None of the three automation scripts references test_bridge_e2e_real
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
AUTOMATION_SCRIPTS = [
    "fast-automation.sh",
    "server-fast-automation.sh",
    "full-automation.sh",
]
MCPB_PATTERN = re.compile(r"mcpb|cidx-bridge|cidx-token-refresh", re.IGNORECASE)


def _script_content(script_name: str) -> str:
    """Return the full text of the named automation script from the repository root."""
    return (REPO_ROOT / script_name).read_text()


def test_installer_directory_does_not_exist_on_disk() -> None:
    """tests/installer/ must be fully removed from the filesystem (Story #778)."""
    installer_path = REPO_ROOT / "tests" / "installer"
    assert not installer_path.exists(), (
        f"tests/installer/ still exists at {installer_path} — remove it with `rm -rf tests/installer/`"
    )


def test_installer_directory_not_tracked_in_git() -> None:
    """tests/installer/ must not appear in git-tracked files (Story #778)."""
    result = subprocess.run(
        ["git", "ls-files", "tests/installer/"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, f"git ls-files failed: {result.stderr}"
    assert result.stdout.strip() == "", (
        f"tests/installer/ still has git-tracked files:\n{result.stdout}"
    )


def test_automation_scripts_contain_no_mcpb_references() -> None:
    """All three automation scripts must have zero MCPB-pattern lines (Story #782).

    Patterns: mcpb (case-insensitive), cidx-bridge, cidx-token-refresh.
    """
    for script_name in AUTOMATION_SCRIPTS:
        content = _script_content(script_name)
        match = MCPB_PATTERN.search(content)
        assert match is None, (
            f"{script_name} still contains MCPB reference '{match.group()}' — "
            f"remove all mcpb/cidx-bridge/cidx-token-refresh lines"
        )


def test_no_orphan_test_bridge_e2e_real_references() -> None:
    """None of the three automation scripts may reference test_bridge_e2e_real (Story #782)."""
    for script_name in AUTOMATION_SCRIPTS:
        content = _script_content(script_name)
        assert "test_bridge_e2e_real" not in content.lower(), (
            f"{script_name} still contains 'test_bridge_e2e_real' — remove the reference"
        )
