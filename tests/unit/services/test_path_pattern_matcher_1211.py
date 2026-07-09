"""
TDD driver for bug #1211: */tests/* misses root-level and deeply-nested paths.

The documented pattern */tests/* (recommended in cli.py help, CLAUDE.md, docs/)
under-filters with pathspec gitwildmatch:
- MATCHES nested:   src/tests/foo.py
- MISSES root:      tests/foo.py        <- BUG
- MISSES deeper:    a/b/tests/foo.py    <- BUG

Fix: normalize a leading */ to **/ so the pattern matches at any depth
including root (same semantics as **/X/*).

Contract after fix:
  */tests/*   matches tests/foo.py (root), src/tests/foo.py (nested-1),
              a/b/tests/foo.py (deep).
  */tests/*   does NOT match mytests/foo.py, contests/x.py (segment boundary kept).
  **/tests/** still works identically (no regression).
  tests/*     still only matches root-level (no regression).
  Both include (--path-filter) and exclude (--exclude-path) use the same matcher
  so both cover root-level after the fix.
"""

import pytest
from code_indexer.services.path_pattern_matcher import PathPatternMatcher


@pytest.fixture()
def matcher() -> PathPatternMatcher:
    return PathPatternMatcher()


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        # --- Bug #1211: */X/* must match root and any depth ---
        ("tests/foo.py", "*/tests/*", True),  # root-level: was False (BUG)
        ("a/b/tests/foo.py", "*/tests/*", True),  # deep nested: was False (BUG)
        ("src/tests/foo.py", "*/tests/*", True),  # one-deep: was already True
        ("src/foo.py", "*/src/*", True),  # root-level src: was False (BUG)
        ("a/b/src/foo.py", "*/src/*", True),  # deep src: was False (BUG)
        # --- No over-match: segment boundaries must be respected ---
        ("mytests/foo.py", "*/tests/*", False),
        ("contests/x.py", "*/tests/*", False),
        ("mysrc/foo.py", "*/src/*", False),
        # --- Regressions: **/tests/** unchanged ---
        ("tests/foo.py", "**/tests/**", True),
        ("src/tests/foo.py", "**/tests/**", True),
        ("a/b/tests/foo.py", "**/tests/**", True),
        ("mytests/foo.py", "**/tests/**", False),
        # --- Regressions: tests/* (no leading */) root-only ---
        ("tests/foo.py", "tests/*", True),
        ("src/tests/foo.py", "tests/*", False),
        # --- Regressions: other pattern types unchanged ---
        ("dist/app.min.js", "*.min.js", True),
        ("test1.py", "test[123].py", True),
        ("test4.py", "test[123].py", False),
        ("test1.py", "test?.py", True),
        ("test12.py", "test?.py", False),
    ],
)
def test_path_pattern_1211(
    matcher: PathPatternMatcher, path: str, pattern: str, expected: bool
) -> None:
    assert matcher.matches_pattern(path, pattern) == expected


@pytest.mark.parametrize(
    "path,patterns,expected",
    [
        # Bug #1211 via matches_any_pattern (include/exclude symmetry)
        ("tests/foo.py", ["*/tests/*"], True),  # root-level via any_pattern
        ("src/tests/foo.py", ["*/tests/*"], True),  # nested via any_pattern
        ("a/b/tests/foo.py", ["*/tests/*"], True),  # deep via any_pattern
        ("mytests/foo.py", ["*/tests/*"], False),  # no over-match
        # Mixed list: covers tests + .min.js
        ("tests/test_foo.py", ["*/tests/*", "*.min.js"], True),
        ("dist/app.min.js", ["*/tests/*", "*.min.js"], True),
        ("src/main.py", ["*/tests/*", "*.min.js"], False),
    ],
)
def test_matches_any_pattern_1211(
    matcher: PathPatternMatcher, path: str, patterns: list, expected: bool
) -> None:
    assert matcher.matches_any_pattern(path, patterns) == expected
