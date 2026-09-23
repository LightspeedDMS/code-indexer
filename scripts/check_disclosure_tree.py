#!/usr/bin/env python3
"""Bug #1916: tree-wide disclosure scan for known PII/system-internal literals.

This is a public open-source repository (see CLAUDE.md "Disclosure Discipline").
The mandatory code-review disclosure scan only ever sees a DIFF -- it can catch
the Nth occurrence of a leaked literal but structurally can never find the
first N-1, because they predate the gate, and it can miss anything added to a
change AFTER reviewers already scanned it (exactly how one of the Bug #1916
leaks landed: both reviewers passed the diff clean, and the offending line was
added in the follow-up round that addressed their findings).

This script closes that gap by scanning the whole GIT-TRACKED TREE (never a
diff) for a small, explicit list of known-leaked literals, and is wired into
BOTH `./lint.sh` (review-time / CI gate) and `.pre-commit-config.yaml`
(commit-time, so a new occurrence cannot even be committed).

Design:
- BANNED_PATTERNS is deliberately small and explicit -- literals PROVEN to
  have leaked before (Bug #1916's operator username). It is NOT a general
  secret-scanner; it exists to make sure a specific, previously-leaked value
  never reappears.
- ALLOWLIST is the documented escape hatch for an occurrence that genuinely
  cannot be scrubbed. Every entry MUST carry a comment explaining why it is
  safe. Adding an entry to silence a real, fixable leak instead of fixing it
  defeats the entire point of this script. Steady state is EMPTY beyond this
  script's own self-reference (it must be able to say what it scans for).
- Binary files are skipped (best-effort UTF-8 decode; anything that fails is
  assumed non-text and skipped, matching how a human reviewer would treat it).
- Only git-TRACKED files are scanned (`git ls-files`) -- this targets what
  actually ships, not scratch/untracked working-tree cruft.

Exit 0: zero non-allowlisted hits. Exit 1: at least one hit, with a
file:line:pattern list printed to stderr.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent

# This file's own path, relative to ROOT, exactly as `git ls-files` prints it.
_SELF_PATH = "scripts/check_disclosure_tree.py"

# ---------------------------------------------------------------------------
# Known-leaked literals (Bug #1916). Each entry: (name, literal, case_sensitive).
# Matching is substring, not regex -- these are exact previously-leaked
# values, not patterns to generalize from.
# ---------------------------------------------------------------------------
BANNED_PATTERNS: List[Tuple[str, str, bool]] = [
    # Bug #1916: operator username leaked in absolute home-directory paths
    # across 33 tracked files (source, tests, shell scripts, reports,
    # CHANGELOG). Scrubbed to the neutral placeholder "opuser" throughout.
    ("operator-username", "jsbattig", False),
    # Bug #1918: operator's real surname leaked (full name, dotted/underscored/
    # concatenated email-local-part and directory-name variants all contain
    # this substring) across ~20 further tracked files. Scrubbed to neutral
    # placeholder identities ("LightspeedDMS" for attribution prose, "Jane
    # Doe"/"jane.doe" for test fixtures) throughout.
    ("operator-full-name", "battig", False),
    # Bug #1918: real corporate email domain, leaked wherever a personal
    # email address on this domain was used as a test/doc fixture (including
    # a THIRD-PARTY name paired with this domain, not just the operator's own
    # address). Scrubbed to the neutral "example.com" domain throughout,
    # except the two genuinely-necessary real contact points (project
    # maintainer email in CODE_OF_CONDUCT.md and package author email in
    # pyproject.toml) which are allowlisted below with a documented reason.
    ("operator-email-domain", "lightspeeddms.com", False),
    # Bug #1918: real DDNS hostname leaked in deployment scripts, docstring
    # examples, and test fixtures across 6 tracked files. Scrubbed to the
    # neutral "cidx.example.com" placeholder throughout -- none of the
    # occurrences were a genuine runtime default (the real default is
    # http://localhost:8000; every hit was illustrative doc/test text).
    ("operator-ddns-hostname", "linner.ddns.net", False),
]

# ---------------------------------------------------------------------------
# Allowlist: {pattern_name: {"path:line" or bare "path" (whole-file exemption)}}.
# A whole-file exemption is used ONLY for a file that must legitimately
# discuss the banned literal itself (this script) -- never for a file that
# merely contains an inconvenient real occurrence. If you are adding an entry
# to fix a lint failure, you are almost certainly doing the wrong thing --
# scrub the occurrence instead, per the classification discipline in Bug
# #1916 (test-fixture vs. sanitiser-fixture vs. doc-prose vs. script-path).
# ---------------------------------------------------------------------------
ALLOWLIST: Dict[str, Set[str]] = {
    "operator-username": {
        # This script necessarily names the literal it searches for.
        _SELF_PATH,
    },
    "operator-full-name": {
        # This script necessarily names the literal it searches for.
        _SELF_PATH,
        # CODE_OF_CONDUCT.md: the project maintainer's real reporting contact.
        # Genuinely needed -- a Code of Conduct report channel must be a real,
        # monitored address; a scrubbed placeholder would silently break the
        # ability of a real reporter to reach a real human (Bug #1918).
        "CODE_OF_CONDUCT.md:11",
        # pyproject.toml: the PyPI package author contact metadata. Same
        # reasoning -- this is real, currently-necessary contact metadata,
        # not an accidental leak, so it is documented here rather than
        # scrubbed to a non-functional value.
        "pyproject.toml:10",
        # --- Intentional authorship attribution (NOT a leak) ---------------
        # A real person's name naming their OWN copyright/authorship of their
        # OWN work is the intended use of that name, not an accidental
        # disclosure. Rewriting a copyright holder is a legal assertion this
        # script has no authority to make; rewriting __author__ or a
        # changelog's own "Contributors" credit line is the same category of
        # mistake. These are documented exemptions, not scrubbed occurrences.
        "LICENSE:3",
        "src/code_indexer/__init__.py:10",
        # NOTE: these three are CHANGELOG "Contributors" credit lines, keyed
        # by line number, so EVERY new changelog entry shifts them down and
        # this list must be re-pointed in the same commit. The 12.67.0 entry
        # moved them by +40 (9648/10019/10500 -> 9688/10059/10540). Tracked
        # as a recurring-toil bug (#1948): the key should be content-based.
        "CHANGELOG.md:9688",
        "CHANGELOG.md:10059",
        "CHANGELOG.md:10540",
    },
    "operator-email-domain": {
        # This script necessarily names the literal it searches for.
        _SELF_PATH,
        "CODE_OF_CONDUCT.md:11",
        "pyproject.toml:10",
    },
    "operator-ddns-hostname": {
        # This script necessarily names the literal it searches for.
        _SELF_PATH,
    },
}


def _tracked_files(root: Path) -> List[str]:
    """Return git-tracked file paths (relative to root)."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    raw = result.stdout.decode("utf-8", errors="surrogateescape")
    return [p for p in raw.split("\0") if p]


def _read_text_or_none(path: Path) -> Optional[str]:
    """Best-effort UTF-8 read; returns None for anything that doesn't decode
    (treated as binary and skipped, same as a human reviewer would)."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def scan(
    root: Path,
    banned_patterns: List[Tuple[str, str, bool]],
    allowlist: Dict[str, Set[str]],
) -> List[str]:
    """Scan the git-tracked tree rooted at `root`; return violation strings.

    `root`, `banned_patterns`, and `allowlist` are all injected (never module
    globals read directly) so this is testable against real temporary git
    repositories without touching the real project tree or its real pattern
    list.
    """
    violations: List[str] = []
    for rel_path in _tracked_files(root):
        allowlisted_whole_file = {
            name for name, paths in allowlist.items() if rel_path in paths
        }
        applicable_patterns = [
            (name, literal, case_sensitive)
            for name, literal, case_sensitive in banned_patterns
            if name not in allowlisted_whole_file
        ]
        if not applicable_patterns:
            continue

        text = _read_text_or_none(root / rel_path)
        if text is None:
            continue

        lines = text.split("\n")
        for name, literal, case_sensitive in applicable_patterns:
            needle = literal if case_sensitive else literal.lower()
            allowed_lines = allowlist.get(name, set())
            for i, line in enumerate(lines, start=1):
                haystack = line if case_sensitive else line.lower()
                if needle not in haystack:
                    continue
                if f"{rel_path}:{i}" in allowed_lines:
                    continue
                violations.append(f"{rel_path}:{i}: [{name}] {line.strip()}")
    return violations


def main() -> int:
    violations = scan(root=ROOT, banned_patterns=BANNED_PATTERNS, allowlist=ALLOWLIST)
    if violations:
        print(
            "Disclosure scan FAILED (Bug #1916): known-leaked literal(s) "
            "found in the tracked tree:",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "\nThis is a public repository. Scrub the occurrence (see "
            "CLAUDE.md 'Disclosure Discipline' and Bug #1916 for "
            "classification discipline) -- do NOT silence this by adding "
            "an ALLOWLIST entry unless the value is genuinely unscrubbable "
            "and the entry is documented with why.",
            file=sys.stderr,
        )
        return 1
    print("Disclosure scan: PASS (zero known-leaked literals in tracked tree)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
