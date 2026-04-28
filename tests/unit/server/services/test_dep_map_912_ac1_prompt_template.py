"""
Story #912 AC1: Prompt template tests.

Verifies that bidirectional_mismatch_audit.md:
- Exists at the correct path
- Contains all 7 required placeholders exactly once
- Can be rendered via str.format(**placeholders)
- Contains the required header marker
- Is not duplicated inline in production source .py files under src/code_indexer/

The no-inline-duplicate test scans src/code_indexer/**/*.py (production modules only,
not tests) for the unique header string "=== BIDIRECTIONAL MISMATCH AUDIT ===" which
would indicate the prompt was erroneously copy-pasted into a Python source file.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent.parent.parent
PROMPTS_DIR = REPO_ROOT / "src" / "code_indexer" / "server" / "mcp" / "prompts"
TEMPLATE_PATH = PROMPTS_DIR / "bidirectional_mismatch_audit.md"
PRODUCTION_SRC_DIR = REPO_ROOT / "src" / "code_indexer"

REQUIRED_PLACEHOLDERS = [
    "source_domain",
    "source_repos",
    "target_domain",
    "target_repos",
    "dep_type",
    "claimed_why",
    "claimed_evidence",
]

FILL_VALUES = {
    "source_domain": "billing-service",
    "source_repos": "billing-repo",
    "target_domain": "payment-gateway",
    "target_repos": "payment-repo",
    "dep_type": "Code-level",
    "claimed_why": "imports PaymentRequest",
    "claimed_evidence": "billing/charge.py:10 PaymentRequest",
}

_UNIQUE_HEADER = "=== BIDIRECTIONAL MISMATCH AUDIT ==="


def test_template_file_exists():
    """AC1: Template file must exist at the specified path."""
    assert TEMPLATE_PATH.exists(), (
        f"bidirectional_mismatch_audit.md not found at {TEMPLATE_PATH}"
    )


def test_template_all_placeholders_present_exactly_once():
    """AC1: All 7 required placeholders must each appear exactly once."""
    content = TEMPLATE_PATH.read_text(encoding="utf-8")
    for placeholder in REQUIRED_PLACEHOLDERS:
        pattern = re.compile(re.escape("{" + placeholder + "}"))
        matches = pattern.findall(content)
        assert len(matches) == 1, (
            f"Placeholder {{{placeholder}}} appears {len(matches)} times — expected exactly 1"
        )


def test_template_format_renders_successfully():
    """AC1: str.format(**placeholders) must succeed without KeyError."""
    content = TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered = content.format(**FILL_VALUES)
    for placeholder in REQUIRED_PLACEHOLDERS:
        assert "{" + placeholder + "}" not in rendered, (
            f"Placeholder {{{placeholder}}} not substituted after format()"
        )


def test_template_contains_required_header():
    """AC1: Template must contain the expected section header line."""
    content = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert _UNIQUE_HEADER in content


def test_no_inline_duplicate_in_production_src():
    """AC1: Prompt header must NOT appear inline in any production .py module.

    Scans src/code_indexer/**/*.py — production source only (no tests).
    Detects accidental copy-paste of the prompt body into a Python file.
    """
    for py_file in PRODUCTION_SRC_DIR.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8", errors="replace")
        assert _UNIQUE_HEADER not in text, (
            f"Inline duplicate of prompt header found in {py_file} — "
            "prompt must live only in bidirectional_mismatch_audit.md"
        )
