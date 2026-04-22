"""Anti-regression tests for schema-version equality comparisons.

Story #885 A4 normalized all lifecycle freshness/version comparisons in src/ to use
'>=' or '<' against CURRENT_LIFECYCLE_SCHEMA_VERSION, never '=='.

RATIONALE: During rolling deploys in a cluster, older nodes write lifecycle files with
schema v3 while newer nodes read them.  If the reader contains:
    `if lifecycle_schema_version == 3:`
the check evaluates False for any future schema version — silently treating valid v3
content as stale.  Using `>=` (forward-compatible) or `<` (detect upgrade needed)
avoids the off-by-one class of bugs that equality comparisons produce during upgrades.

These tests enforce that the invariant is never re-introduced in src/.
"""

import subprocess
from pathlib import Path

# Resolve src/ relative to this file so the test works from any working directory.
_SRC_DIR = str(Path(__file__).resolve().parents[3] / "src")


def _run_grep(pattern: str) -> subprocess.CompletedProcess:
    """Run grep with the given extended-regex pattern against src/.

    Returns the CompletedProcess so callers can inspect returncode and stdout.
    grep exit code 0 = matches found, 1 = no matches, 2 = error.
    """
    return subprocess.run(
        ["grep", "-rnE", pattern, _SRC_DIR],
        capture_output=True,
        text=True,
    )


def test_no_equality_comparisons_against_schema_version_numeric_literal():
    """Enforce: no 'lifecycle_schema_version == <N>' comparisons in src/.

    Equality checks against a hardcoded integer are fragile during rolling deploys
    where older nodes write v3 and newer nodes read it.  Use '>=' or '<' instead.
    """
    result = _run_grep(r"lifecycle_schema_version\s*==\s*[0-9]+")

    assert result.returncode == 1, (
        "Found forbidden equality comparison(s) against numeric schema version "
        "literal in src/:\n"
        + result.stdout
        + "\nFix: replace '== N' with '>= N' or '< N' as appropriate."
    )
    assert result.stdout == "", (
        "grep returned no-match exit code but produced output — unexpected:\n"
        + result.stdout
    )


def test_no_equality_comparisons_against_schema_version_constant():
    """Enforce: no 'lifecycle_schema_version == CURRENT_LIFECYCLE_SCHEMA_VERSION' in src/.

    Even comparing against the named constant with '==' is brittle: a document written
    at version N-1 is valid but the check rejects it.  Use '>=' for forward-compatible
    reads, '<' to detect docs that need an upgrade.
    """
    result = _run_grep(
        r"lifecycle_schema_version\s*==\s*CURRENT_LIFECYCLE_SCHEMA_VERSION"
    )

    assert result.returncode == 1, (
        "Found forbidden equality comparison(s) against CURRENT_LIFECYCLE_SCHEMA_VERSION "
        "in src/:\n"
        + result.stdout
        + "\nFix: replace '== CURRENT_LIFECYCLE_SCHEMA_VERSION' with "
        "'>= CURRENT_LIFECYCLE_SCHEMA_VERSION' or compare with '<' as appropriate."
    )
    assert result.stdout == "", (
        "grep returned no-match exit code but produced output — unexpected:\n"
        + result.stdout
    )
