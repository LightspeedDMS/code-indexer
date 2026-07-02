"""
Unit tests for Story #929 Item #17 — documentation accuracy.

Tests verify that docs/operating-modes.md has been updated to reflect the
Research Assistant's actual remediation authority and scope boundaries, and no
longer misleadingly describes the RA as a read-only code investigation tool.
"""

from pathlib import Path

import pytest

_DOCS_PATH = Path(__file__).parent.parent.parent / "docs" / "operating-modes.md"


def _extract_ra_section(docs_content: str) -> str:
    """
    Extract the Research Assistant subsection from operating-modes.md.

    Finds the heading line containing "Research Assistant" and returns
    the content up to (but not including) the next markdown heading of
    equal or higher level.  Raises AssertionError if the section is absent.
    """
    lines = docs_content.splitlines(keepends=True)
    start_idx: int = -1
    heading_level: int = 0

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") and "research assistant" in stripped.lower():
            # Count leading '#' characters to determine heading level
            heading_level = len(stripped) - len(stripped.lstrip("#"))
            start_idx = i
            break

    assert start_idx != -1, (
        "Could not find a 'Research Assistant' heading in operating-modes.md. "
        "The section must exist for the tests to validate its content."
    )

    # Collect lines until a heading at the same level or higher is encountered
    section_lines = [lines[start_idx]]
    for line in lines[start_idx + 1 :]:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            current_level = len(stripped) - len(stripped.lstrip("#"))
            if current_level <= heading_level:
                break
        section_lines.append(line)

    return "".join(section_lines)


@pytest.fixture
def ra_section() -> str:
    """Return only the Research Assistant section of operating-modes.md."""
    assert _DOCS_PATH.exists(), f"operating-modes.md not found at {_DOCS_PATH}."
    return _extract_ra_section(_DOCS_PATH.read_text())


class TestOperatingModesDocumentationAccuracy:
    """Item #17: docs/operating-modes.md must describe RA remediation authority."""

    def test_remediation_authority_in_ra_section(self, ra_section):
        """The RA section must contain both 'remediation' and 'authority'."""
        lower = ra_section.lower()
        assert "remediation" in lower, (
            "The Research Assistant section must mention 'remediation' (Item #17). "
            f"Got RA section:\n{ra_section}"
        )
        assert "authority" in lower, (
            "The Research Assistant section must mention 'authority' to describe "
            "the RA's elevated capabilities (Item #17). "
            f"Got RA section:\n{ra_section}"
        )

    def test_scope_boundaries_in_ra_section(self, ra_section):
        """The RA section must mention scope to describe operational boundaries."""
        assert "scope" in ra_section.lower(), (
            "The Research Assistant section must mention 'scope' to describe "
            "operational boundaries (Item #17). "
            f"Got RA section:\n{ra_section}"
        )

    def test_ra_section_describes_write_capability(self, ra_section):
        """
        The RA section must describe write/remediation capability, contradicting
        the old read-only framing of the RA as purely a code investigation tool.
        """
        lower = ra_section.lower()
        has_write_capability = any(
            word in lower for word in ("remediat", "repair", "write", "fix", "modify")
        )
        assert has_write_capability, (
            "The Research Assistant section must describe write/remediation capability "
            "(Item #17). The old framing described it as read-only investigation only. "
            f"Got RA section:\n{ra_section}"
        )
