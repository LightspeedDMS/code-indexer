"""AC-V4-15 tests for lifecycle_unified.md Section 6 (schema v4 clauses).

Story #885 A1+A9a adds Section 6 to lifecycle_unified.md declaring the v4
`environments` and `branch_environment_map` fields, including three load-bearing
clauses:

1. No query budget cap on cidx-local (precision > query count)
2. ANTI-RULE: branch-name coincidence is NOT evidence for branch_environment_map
3. YAML quoting requirement for scoped/reserved-char scalar values (A9a)

These tests encode those clauses as invariants so they cannot be silently removed.

test_no_runtime_code_enforces_cidx_local_cap additionally scans
src/code_indexer/global_repos/lifecycle_*.py to confirm the unbounded-query
decision from workshop decision #3 is not violated in runtime code.
"""

import re
import subprocess
from pathlib import Path

_PROMPT_FILE = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "prompts"
    / "lifecycle_unified.md"
)

_LIFECYCLE_SRC = str(
    Path(__file__).resolve().parents[4] / "src" / "code_indexer" / "global_repos"
)


def _read_prompt() -> str:
    return _PROMPT_FILE.read_text(encoding="utf-8")


def _extract_section6(content: str) -> str:
    """Return only the Section 6 text, bounded by the next numbered section heading.

    Uses multiline + DOTALL mode so '^' anchors to real line starts.
    Matches from a line starting with '**6.' up to (but not including) the next
    line starting with '**<digit>.' or end of string.
    Returns empty string if Section 6 is not present.
    """
    match = re.search(
        r"(?ms)(^\*\*6\..*?)(?=^\*\*\d+\.|\Z)",
        content,
    )
    return match.group(1) if match else ""


def test_prompt_contains_no_budget_cap_clause():
    """Section 6 must state that cidx-local queries are unbounded.

    The 'no query budget cap' clause authorises Claude to investigate as many
    sibling repos as precision requires.  Without this clause, agents may
    self-limit queries and miss branch->environment evidence.
    """
    content = _read_prompt()
    section6 = _extract_section6(content)
    assert section6, (
        "lifecycle_unified.md is missing Section 6 entirely.  "
        "Add Section 6 per Story #885 A1."
    )
    assert "no query budget cap on cidx-local" in section6.lower(), (
        "Section 6 of lifecycle_unified.md is missing the "
        "'no query budget cap on cidx-local' clause required by Story #885 A1."
    )


def test_prompt_contains_anti_coincidence_rule():
    """Section 6 must contain the ANTI-RULE against branch-name coincidence.

    The ANTI-RULE guards against Claude hallucinating branch->environment mappings
    based solely on branch naming conventions (e.g. a branch called 'dev' does NOT
    prove a 'dev' environment is mapped to it).  Explicit deploy-wiring evidence
    is required.
    """
    content = _read_prompt()
    section6 = _extract_section6(content)
    assert section6, (
        "lifecycle_unified.md is missing Section 6 entirely.  "
        "Add Section 6 per Story #885 A1."
    )
    assert "ANTI-RULE" in section6, (
        "Section 6 of lifecycle_unified.md is missing the 'ANTI-RULE' marker "
        "required by Story #885 A1."
    )
    assert "branch-name coincidence is NOT evidence" in section6, (
        "Section 6 of lifecycle_unified.md is missing the phrase "
        "'branch-name coincidence is NOT evidence' required by Story #885 A1."
    )


def test_prompt_does_not_set_numeric_cap():
    """Section 6 must NOT impose a numeric query cap on cidx-local queries.

    Workshop decision #3: unbounded cidx-local queries — precision > query count.
    Phrases like 'maximum 5', 'at most N', 'stop after N', or 'limit N'
    in Section 6 would contradict this.  Scoped to Section 6 only to avoid
    false positives from numeric references in other sections.
    """
    content = _read_prompt()
    section6 = _extract_section6(content)
    if not section6:
        # Section 6 absent — other tests will catch the missing section.
        return

    forbidden_patterns = [
        r"maximum\s+\d+",
        r"at most\s+\d+",
        r"stop after\s+\d+",
        r"\blimit\s+\d+",
    ]
    for pattern in forbidden_patterns:
        match = re.search(pattern, section6, re.IGNORECASE)
        assert match is None, (
            f"Section 6 of lifecycle_unified.md contains a forbidden numeric cap "
            f"pattern '{pattern}' matched at: '{match.group(0)}'.  "
            f"Remove numeric caps on cidx-local queries per Story #885 A1 "
            f"workshop decision #3."
        )


def test_no_runtime_code_enforces_cidx_local_cap():
    """No runtime Python code in lifecycle_*.py may enforce a cidx-local query cap.

    Workshop decision #3 says the cap is absent — enforcing it in code would
    silently contradict the prompt.  Two grep passes cover both term orderings:
      - cap-term before cidx-local token  (e.g. 'limit ... cidx_local')
      - cidx-local token before cap-term  (e.g. 'cidx-local ... max_queries')
    Both 'cidx-local' and 'cidx_local' variants are matched via 'cidx[-_]local'.
    """
    cap_terms = r"(budget|cap|limit|max_queries|query_count)"
    cidx_local = r"cidx[-_]local"

    patterns = [
        f"{cap_terms}.*{cidx_local}",  # cap-term first
        f"{cidx_local}.*{cap_terms}",  # cidx-local first
    ]

    all_matches: list[str] = []
    for pattern in patterns:
        result = subprocess.run(
            [
                "grep",
                "-rnE",
                pattern,
                _LIFECYCLE_SRC,
                "--include=lifecycle_*.py",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            all_matches.append(result.stdout.strip())

    assert not all_matches, (
        "Found runtime cidx-local query cap enforcement in lifecycle_*.py:\n"
        + "\n".join(all_matches)
        + "\nThis contradicts Story #885 workshop decision #3 (unbounded queries).  "
        "Remove the cap or justify via a story amendment."
    )


def test_prompt_contains_yaml_quoting_requirement():
    """Section 6 must contain the YAML quoting requirement (A9a).

    Scoped npm package names like '@org/pkg' start with '@', a YAML reserved
    indicator.  Without quoting, 'yaml.safe_load()' raises ScannerError on
    round-trip.  The prompt must instruct Claude to wrap such values in double
    quotes.
    """
    content = _read_prompt()
    section6 = _extract_section6(content)
    assert section6, (
        "lifecycle_unified.md is missing Section 6 entirely.  "
        "Add Section 6 per Story #885 A9a."
    )
    assert "yaml quoting" in section6.lower(), (
        "Section 6 of lifecycle_unified.md is missing the 'YAML quoting' "
        "requirement clause required by Story #885 A9a."
    )
