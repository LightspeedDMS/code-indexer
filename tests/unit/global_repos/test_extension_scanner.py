"""Unit tests for Story #1001: has_files_with_extensions() scanner function.

Tests verify that has_files_with_extensions() correctly scans a directory tree
and returns True (short-circuiting) as soon as a file with a matching extension
is found, or False if no match exists.

Accepted behaviors verified:
- Basic match/no-match/empty-extension scenarios
- Short-circuit: scanning stops at first match (verified by controlling walk order)
- Hidden directory auto-skip: dirs starting with '.' are always skipped,
  regardless of exclude_dirs contents
- Explicit exclude_dirs pruning: dirs in the set are never walked
- Nested match: files inside subdirectories are found
"""

from typing import List, Tuple
from unittest.mock import patch

import pytest


def _get_scanner():
    """Import has_files_with_extensions from refresh_scheduler."""
    from code_indexer.global_repos.refresh_scheduler import has_files_with_extensions

    return has_files_with_extensions


STANDARD_EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".code-indexer",
    ".versioned",
}


# ---------------------------------------------------------------------------
# Tests: basic match / no-match / empty-extension
# ---------------------------------------------------------------------------


class TestHasFilesBasicBehavior:
    """Basic match/no-match/empty-extension scenarios in a flat directory."""

    @pytest.mark.parametrize(
        "filenames, extensions, expected",
        [
            (["main.py", "data.jsonl"], {"jsonl"}, True),
            (["app.log", "main.py"], {"log"}, True),
            (["main.py", "app.js"], {"jsonl"}, False),
            (["main.py", "data.jsonl"], set(), False),
            ([], {"py"}, False),
        ],
    )
    def test_basic_scenarios(self, tmp_path, filenames, extensions, expected):
        """Returns correct True/False for files in a flat directory."""
        scanner = _get_scanner()
        for fn in filenames:
            (tmp_path / fn).write_text("content")

        result = scanner(str(tmp_path), extensions, exclude_dirs=set())
        assert result is expected


# ---------------------------------------------------------------------------
# Tests: short-circuit via controlled fake os.walk
# ---------------------------------------------------------------------------


class TestShortCircuit:
    """Scanning must stop as soon as the first matching file is found.

    We inject a fake os.walk generator that records which entries were consumed.
    If the implementation is correct, it requests at most the entries up to and
    including the first matching file, and never requests subsequent entries.
    """

    def test_stops_after_first_match(self, tmp_path):
        """Scanner consumes at most the entry containing the first match.

        The fake walk yields two entries in deterministic order:
          Entry 0 (root): contains the matching file
          Entry 1 (sub):  would only be yielded if traversal continues

        A correct short-circuit implementation must NOT consume Entry 1.
        """
        scanner = _get_scanner()
        root_str = str(tmp_path)
        sub_str = str(tmp_path / "sub")

        # Fake walk yields root first (contains match), then sub
        walk_entries: List[Tuple] = [
            (root_str, ["sub"], ["match.jsonl"]),
            (sub_str, [], ["other.py"]),
        ]
        consumed: List[str] = []

        def fake_walk(top, topdown=True, **kwargs):
            for dirpath, dirnames, filenames in walk_entries:
                consumed.append(dirpath)
                yield dirpath, list(dirnames), list(filenames)

        with patch(
            "code_indexer.global_repos.refresh_scheduler.os.walk",
            side_effect=fake_walk,
        ):
            result = scanner(root_str, {"jsonl"}, exclude_dirs=set())

        assert result is True
        assert sub_str not in consumed, (
            f"Short-circuit failed: scanner consumed entry for '{sub_str}' "
            "even though a match was already found in the root entry"
        )


# ---------------------------------------------------------------------------
# Tests: hidden directory auto-skip
# ---------------------------------------------------------------------------


class TestHiddenDirAutoSkip:
    """Directories starting with '.' must be skipped automatically.

    This must hold regardless of whether exclude_dirs is empty or non-empty.
    """

    def test_hidden_dir_not_walked_with_empty_exclude_dirs(self, tmp_path):
        """Hidden dir starting with '.' is skipped even when exclude_dirs=set()."""
        scanner = _get_scanner()
        hidden = tmp_path / ".hidden_data"
        hidden.mkdir()
        (hidden / "only_match.jsonl").write_text("content")
        (tmp_path / "unrelated.py").write_text("content")

        result = scanner(str(tmp_path), {"jsonl"}, exclude_dirs=set())
        assert result is False, (
            "Hidden dir '.hidden_data' should be auto-skipped regardless of exclude_dirs; "
            "the match inside it must not be found"
        )

    def test_hidden_dir_not_walked_with_nonempty_exclude_dirs_that_omits_it(
        self, tmp_path
    ):
        """Hidden dir is skipped even when exclude_dirs is non-empty but doesn't list it."""
        scanner = _get_scanner()
        hidden = tmp_path / ".hidden_data"
        hidden.mkdir()
        (hidden / "only_match.jsonl").write_text("content")
        # exclude_dirs has node_modules but NOT .hidden_data
        result = scanner(str(tmp_path), {"jsonl"}, exclude_dirs={"node_modules"})
        assert result is False, (
            "Hidden dir '.hidden_data' must still be skipped when it is not "
            "listed in exclude_dirs but starts with '.'"
        )

    def test_visible_sibling_still_matched_when_hidden_dir_skipped(self, tmp_path):
        """Visible sibling dir is scanned normally while hidden dirs are skipped."""
        scanner = _get_scanner()
        hidden = tmp_path / ".hidden_data"
        hidden.mkdir()
        (hidden / "skip_me.jsonl").write_text("content")
        visible = tmp_path / "src"
        visible.mkdir()
        (visible / "data.jsonl").write_text("content")

        result = scanner(str(tmp_path), {"jsonl"}, exclude_dirs=set())
        assert result is True


# ---------------------------------------------------------------------------
# Tests: explicit exclude_dirs pruning
# ---------------------------------------------------------------------------


class TestExcludeDirPruning:
    """Directories listed in exclude_dirs are never walked."""

    @pytest.mark.parametrize("excluded_dir", sorted(STANDARD_EXCLUDE_DIRS))
    def test_standard_exclude_dirs_are_pruned(self, tmp_path, excluded_dir):
        """Each standard exclude dir is not walked when present in exclude_dirs."""
        scanner = _get_scanner()
        excl_path = tmp_path / excluded_dir
        excl_path.mkdir()
        (excl_path / "match.jsonl").write_text("content")

        result = scanner(str(tmp_path), {"jsonl"}, exclude_dirs=STANDARD_EXCLUDE_DIRS)
        assert result is False, (
            f"Dir '{excluded_dir}' must be excluded; match inside it must not be found"
        )

    def test_non_excluded_dir_is_scanned(self, tmp_path):
        """Non-excluded directories are walked and matches are found."""
        scanner = _get_scanner()
        src = tmp_path / "src"
        src.mkdir()
        (src / "data.jsonl").write_text("content")

        result = scanner(str(tmp_path), {"jsonl"}, exclude_dirs={"node_modules"})
        assert result is True
