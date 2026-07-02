"""
Bug #720: Replace all `.replace("-global", "")` with `.removesuffix("-global")`.

The bug: `.replace("-global", "")` removes ALL occurrences of `-global` from the string.
For aliases like `r53-global-global`, this produces `r53` instead of the correct `r53-global`.

The fix: `.removesuffix("-global")` (Python 3.9+) only strips the trailing suffix.
"""

import re
from pathlib import Path

import pytest

BANNED_PATTERN = re.compile(r'\.replace\("-global",\s*""\)')
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # repo root

FILES_TO_CHECK = [
    "src/code_indexer/global_repos/alias_manager.py",
    "src/code_indexer/global_repos/refresh_scheduler.py",
    "src/code_indexer/server/mcp/handlers/_legacy.py",
    "src/code_indexer/server/mcp/handlers/files.py",
    "src/code_indexer/server/mcp/handlers/search.py",
    "src/code_indexer/server/mcp/handlers/git_read.py",
]


def _banned_lines(rel_path: str) -> list[int]:
    """Return line numbers that still use the banned .replace("-global", "") pattern."""
    file_path = PROJECT_ROOT / rel_path
    return [
        i
        for i, line in enumerate(file_path.read_text().splitlines(), start=1)
        if BANNED_PATTERN.search(line)
    ]


@pytest.mark.parametrize("rel_path", FILES_TO_CHECK)
def test_no_replace_global_in_source_file(rel_path: str) -> None:
    """Every listed source file must use .removesuffix("-global"), not .replace("-global", "")."""
    bad_lines = _banned_lines(rel_path)
    assert bad_lines == [], (
        f'{rel_path} still uses .replace("-global", "") on lines: {bad_lines}'
    )


@pytest.mark.parametrize(
    "alias, expected",
    [
        # Single trailing suffix — both operations agree
        ("my-repo-global", "my-repo"),
        # No suffix at all — both operations agree (no change)
        ("my-repo", "my-repo"),
    ],
)
def test_removesuffix_matches_replace_for_safe_cases(alias: str, expected: str) -> None:
    """For simple aliases both .replace() and .removesuffix() produce the same result."""
    assert alias.replace("-global", "") == expected
    assert alias.removesuffix("-global") == expected


@pytest.mark.parametrize(
    "alias, wrong_via_replace, correct_via_removesuffix",
    [
        # Double suffix: replace strips both, removesuffix strips only the trailing one
        ("r53-global-global", "r53", "r53-global"),
        # Embedded -global in base name: replace destroys it, removesuffix leaves it intact
        ("aws-global-route53-global", "aws-route53", "aws-global-route53"),
    ],
)
def test_replace_diverges_from_removesuffix_for_bug_cases(
    alias: str, wrong_via_replace: str, correct_via_removesuffix: str
) -> None:
    """Demonstrate the bug: .replace() is destructive for aliases with embedded -global."""
    assert alias.replace("-global", "") == wrong_via_replace, (
        "replace() must produce the wrong result to prove the bug exists"
    )
    assert alias.removesuffix("-global") == correct_via_removesuffix, (
        "removesuffix() must produce the correct result"
    )
