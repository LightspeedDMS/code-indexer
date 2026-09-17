"""Bug #1876 item 4 re-measurement follow-up: ``_normalize_path`` per-path cost.

The code-review re-measurement (over 8,000 paths) found the shared
``PathSelector``/``CompiledPatternSet`` machinery running ~5x slower than a
bare, prebuilt ``pathspec.PathSpec.match_file()`` call -- profiling traced
essentially all of the gap to ``PathPatternMatcher._normalize_path``
unconditionally building a ``PurePosixPath`` (parse + rebuild parts) on
EVERY single call, even though the overwhelming majority of paths reaching
this per-file hot path (relative paths from ``os.walk``/ripgrep) are already
clean POSIX-relative strings with no backslashes, no ``.``/``..``
components, and no repeated slashes.

This file proves two things about the fix:

1. Correctness is unchanged: the fast path must return byte-identical
   results to the slow ``PurePosixPath``-based path for every case the slow
   path exists to handle (backslashes, ``.``/``..`` components in every
   position, repeated slashes, absolute paths, already-clean paths).
2. The fast path is actually faster: already-clean paths must be
   meaningfully cheaper to normalize than paths that need real ``.``/``..``
   resolution, ruling out a "fast path" that silently never triggers.
"""

from __future__ import annotations

import time

import pytest

from code_indexer.services.path_pattern_matcher import PathPatternMatcher


@pytest.fixture
def matcher() -> PathPatternMatcher:
    return PathPatternMatcher()


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Already-clean paths -- these are the fast-path candidates.
        ("src/pkg/file.py", "src/pkg/file.py"),
        ("file.py", "file.py"),
        ("a/b/c/d/e.rs", "a/b/c/d/e.rs"),
        ("/abs/path/file.py", "/abs/path/file.py"),
        ("", ""),
        # Backslashes must still be converted.
        ("src\\tests\\test.py", "src/tests/test.py"),
        # `.` components in every position must still be dropped.
        (".", ""),
        ("./a/b.py", "a/b.py"),
        ("a/./b.py", "a/b.py"),
        ("a/b/.", "a/b"),
        ("a/./b/./c.py", "a/b/c.py"),
        # `..` components must still resolve against a preceding segment.
        ("a/b/../c.py", "a/c.py"),
        ("a/../b.py", "b.py"),
        ("../a.py", "../a.py"),
        ("a/../../b.py", "../b.py"),
        # Repeated slashes must still collapse.
        ("a//b.py", "a/b.py"),
        ("a///b//c.py", "a/b/c.py"),
        # Trailing slash must still be dropped (directory marker).
        ("a/b/", "a/b"),
        # Root alone.
        ("/", "/"),
    ],
)
def test_normalize_path_fast_path_matches_slow_path_result(matcher, raw, expected):
    """The optimized fast path must never change the normalized value."""
    assert matcher._normalize_path(raw) == expected


def test_normalize_path_already_clean_paths_are_faster_than_dotted_paths(matcher):
    """The fast path must actually trigger, not merely exist unreachable.

    Compares already-clean relative paths (fast-path eligible) against
    paths requiring real ``.``/``..`` resolution (must take the slow,
    PurePosixPath-based path) over the same volume and shape of input.  A
    relative-timing comparison (rather than an absolute millisecond bound)
    keeps this robust across slower/faster CI hardware.
    """
    clean_paths = [f"src/pkg{i}/file_{i}.py" for i in range(4000)]
    dotted_paths = [f"src/./pkg{i}/../pkg{i}/file_{i}.py" for i in range(4000)]

    # Warm up (module import, attribute lookups) before timing either loop.
    for p in clean_paths[:50]:
        matcher._normalize_path(p)
    for p in dotted_paths[:50]:
        matcher._normalize_path(p)

    start = time.perf_counter()
    for p in clean_paths:
        matcher._normalize_path(p)
    clean_elapsed = time.perf_counter() - start

    start = time.perf_counter()
    for p in dotted_paths:
        matcher._normalize_path(p)
    dotted_elapsed = time.perf_counter() - start

    assert clean_elapsed < dotted_elapsed * 0.7, (
        "expected already-clean paths to take a fast path meaningfully "
        f"cheaper than paths needing real ./.. resolution, got "
        f"clean={clean_elapsed:.4f}s dotted={dotted_elapsed:.4f}s"
    )
