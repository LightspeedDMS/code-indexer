"""
Regression test for Bundle 4 of epic #756 (stories #794 + #830 + #832 + #833):
- Scrub MCPB references from user-facing docs (#794)
- Delete docs/mcpb/ directory (#830)
- Add MCPB removal entry to CHANGELOG.md (#832)
- Update CLAUDE.md project-memory references to MCPB (#833)
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text()


def test_docs_mcpb_directory_does_not_exist() -> None:
    target = REPO_ROOT / "docs" / "mcpb"
    assert not target.exists(), (
        f"docs/mcpb/ still exists at {target} — delete via `git rm -r docs/mcpb/`"
    )


def test_readme_has_no_docs_mcpb_reference() -> None:
    content = _read("README.md")
    assert "docs/mcpb/" not in content


def test_server_deployment_has_no_mcpb_link() -> None:
    content = _read("docs/server-deployment.md")
    assert "mcpb/" not in content


def test_installation_has_no_mcpb_bridge_reference() -> None:
    content = _read("docs/installation.md")
    assert "mcpb/bridge.py" not in content


def test_ai_integration_has_no_mcpb_config_path() -> None:
    content = _read("docs/ai-integration.md")
    assert "~/.mcpb/config.json" not in content


def test_claude_md_no_docs_mcpb_setup_reference() -> None:
    content = _read("CLAUDE.md")
    assert "docs/mcpb/setup.md" not in content


def test_claude_md_no_mcpb_init_reference() -> None:
    content = _read("CLAUDE.md")
    assert "mcpb/__init__.py" not in content


def test_changelog_contains_mcpb_removal_entry() -> None:
    content = _read("CHANGELOG.md")
    assert "MCPB Removed (epic #756)" in content
