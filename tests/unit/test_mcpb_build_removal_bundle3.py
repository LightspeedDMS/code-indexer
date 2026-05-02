"""
Regression test for Bundle 3 of epic #756 (stories #828 + #829):
- Delete MCPB installer scripts (#828)
- Delete release-mcpb.yml CI workflow + add RELEASE_NOTES.md migration note (#829)

Asserts the post-deletion invariants stay locked.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def _release_notes_content() -> str:
    return (REPO_ROOT / "RELEASE_NOTES.md").read_text()


def test_install_mcpb_script_does_not_exist() -> None:
    target = REPO_ROOT / "install-mcpb.sh"
    assert not target.exists(), (
        f"install-mcpb.sh still exists at {target} — delete it via `git rm install-mcpb.sh`"
    )


def test_setup_mcpb_script_does_not_exist() -> None:
    target = REPO_ROOT / "scripts" / "setup-mcpb.sh"
    assert not target.exists(), f"scripts/setup-mcpb.sh still exists at {target}"


def test_nsis_installer_script_does_not_exist() -> None:
    target = REPO_ROOT / "scripts" / "installer" / "mcpb-installer.nsi"
    assert not target.exists(), (
        f"scripts/installer/mcpb-installer.nsi still exists at {target}"
    )


def test_nsis_installer_readme_does_not_exist() -> None:
    target = REPO_ROOT / "scripts" / "installer" / "README.md"
    assert not target.exists(), (
        f"scripts/installer/README.md still exists at {target} — "
        "the installer README is 100% MCPB-specific and must be deleted with the .nsi file"
    )


def test_installer_directory_removed_when_empty() -> None:
    target = REPO_ROOT / "scripts" / "installer"
    assert not target.exists(), (
        f"scripts/installer/ directory still exists at {target} — "
        "should be removed entirely once both MCPB-only files inside are deleted"
    )


def test_release_mcpb_workflow_does_not_exist() -> None:
    target = REPO_ROOT / ".github" / "workflows" / "release-mcpb.yml"
    assert not target.exists(), (
        f".github/workflows/release-mcpb.yml still exists at {target}"
    )


def test_release_notes_contains_mcpb_removal_migration_note() -> None:
    content = _release_notes_content()
    assert "MCPB removed" in content, (
        "RELEASE_NOTES.md must contain a migration note with the substring "
        "'MCPB removed' directing past MCPB users to the server's native "
        "/mcp and /mcp-public endpoints (story #829 owns this note)."
    )
