"""Tests for the shared indexing containment primitive
``code_indexer.utils.path_confinement.is_resolved_within_root``.

This is the single helper every file-discovery and file-read path in the
indexer uses to decide whether a candidate file's resolved location (after
following symlinks and collapsing ``..`` segments) lies inside the resolved
codebase root. Real filesystem operations throughout (CLAUDE.md Foundation
#1) -- no mocks.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.utils.path_confinement import (
    is_resolved_within_root,
    resolve_if_within_root,
)


class TestIsResolvedWithinRoot:
    def test_plain_in_root_file_is_within_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        inside_file = root / "a.txt"
        inside_file.write_text("content")

        assert is_resolved_within_root(inside_file, root.resolve()) is True

    def test_symlink_to_outside_file_is_not_within_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("content")
        link = root / "link.txt"
        link.symlink_to(outside_file)

        assert is_resolved_within_root(link, root.resolve()) is False

    def test_symlink_to_inside_file_is_within_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        inside_target = root / "real.txt"
        inside_target.write_text("content")
        link = root / "link.txt"
        link.symlink_to(inside_target)

        assert is_resolved_within_root(link, root.resolve()) is True

    def test_parent_component_is_not_within_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("content")
        candidate = root / ".." / "outside.txt"

        assert is_resolved_within_root(candidate, root.resolve()) is False

    def test_symlink_loop_is_not_within_root_and_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        loop_a = root / "a.py"
        loop_b = root / "b.py"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)

        assert is_resolved_within_root(loop_a, root.resolve()) is False

    def test_broken_symlink_pointing_outside_root_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        broken_link = root / "broken.py"
        broken_link.symlink_to(tmp_path / "does_not_exist_outside.py")

        assert is_resolved_within_root(broken_link, root.resolve()) is False

    def test_broken_symlink_pointing_inside_root_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        """A broken link whose lexical target is still under root resolves
        (Path.resolve() does not require existence) to a location inside
        root -- containment holds even though the target file itself does
        not exist; the missing file is caught later by a stat()/open()
        failure, not by this containment primitive."""
        root = tmp_path / "root"
        root.mkdir()
        broken_link = root / "broken_inside.py"
        broken_link.symlink_to(root / "does_not_exist_inside.py")

        assert is_resolved_within_root(broken_link, root.resolve()) is True

    def test_nonexistent_plain_candidate_resolves_by_lexical_path(
        self, tmp_path: Path
    ) -> None:
        """A candidate that does not exist at all (no symlink involved)
        still resolves lexically under root -- resolve() does not require
        existence -- so containment is still correctly reported True."""
        root = tmp_path / "root"
        root.mkdir()
        candidate = root / "never_created.py"

        assert is_resolved_within_root(candidate, root.resolve()) is True


class TestResolveIfWithinRoot:
    """``resolve_if_within_root`` is the core implementation --
    ``is_resolved_within_root`` is a thin boolean wrapper around it, so
    callers that need the resolved value (not just a yes/no) get it
    without a second, duplicate resolve() call."""

    def test_in_root_candidate_returns_its_resolved_path(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        target = root / "a.txt"
        target.write_text("content")

        result = resolve_if_within_root(target, root.resolve())

        assert result == target.resolve()

    def test_out_of_root_candidate_returns_none(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("content")
        link = root / "link.txt"
        link.symlink_to(outside_file)

        assert resolve_if_within_root(link, root.resolve()) is None

    def test_symlink_loop_returns_none_without_raising(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        loop_a = root / "a.py"
        loop_b = root / "b.py"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)

        assert resolve_if_within_root(loop_a, root.resolve()) is None

    def test_is_resolved_within_root_is_consistent_with_resolve_if_within_root(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        inside = root / "inside.txt"
        inside.write_text("x")
        outside = tmp_path / "outside.txt"
        outside.write_text("x")

        resolved_root = root.resolve()
        assert is_resolved_within_root(inside, resolved_root) is (
            resolve_if_within_root(inside, resolved_root) is not None
        )
        assert is_resolved_within_root(outside, resolved_root) is (
            resolve_if_within_root(outside, resolved_root) is not None
        )
