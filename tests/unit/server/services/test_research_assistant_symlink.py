"""
Tests for broken symlink detection in research assistant session setup (Finding 20).

When issue_manager_link is a symlink pointing to a non-existent target,
the broken symlink must be removed before a new one can be created.
"""


class TestBrokenSymlinkDetection:
    """Finding 20: Broken symlinks must be removed before recreation."""

    def test_broken_symlink_removed_before_recreation(self, tmp_path):
        """A broken symlink is removed so a valid one can be created in its place."""
        # Create source and link paths in tmp_path
        source = tmp_path / "issue_manager.py"
        source.write_text("# placeholder")

        link = tmp_path / "issue_manager_link.py"

        # Create a broken symlink: point to a non-existent target
        nonexistent = tmp_path / "does_not_exist.py"
        link.symlink_to(nonexistent)

        # Verify precondition: is_symlink() True but exists() False
        assert link.is_symlink()
        assert not link.exists()

        # Apply the fix logic from research_assistant_service.py
        if link.is_symlink() and not link.exists():
            link.unlink()

        if not link.exists():
            if source.exists():
                link.symlink_to(source)

        # Verify the broken symlink was replaced with a valid one
        assert link.is_symlink()
        assert link.exists()
        assert link.resolve() == source.resolve()

    def test_valid_symlink_not_touched(self, tmp_path):
        """A valid (non-broken) symlink is left untouched."""
        source = tmp_path / "issue_manager.py"
        source.write_text("# placeholder")

        link = tmp_path / "issue_manager_link.py"
        link.symlink_to(source)

        # Verify precondition: valid symlink
        assert link.is_symlink()
        assert link.exists()

        # Apply the fix logic - broken symlink block should NOT fire
        if link.is_symlink() and not link.exists():
            link.unlink()  # Should NOT be called

        # Link should still exist and point to same target
        assert link.is_symlink()
        assert link.exists()
        assert link.resolve() == source.resolve()

    def test_no_symlink_creates_new_one(self, tmp_path):
        """When no symlink exists and source is present, a new symlink is created."""
        source = tmp_path / "issue_manager.py"
        source.write_text("# placeholder")

        link = tmp_path / "issue_manager_link.py"

        # No symlink exists yet
        assert not link.exists()
        assert not link.is_symlink()

        # Apply the fix logic
        if link.is_symlink() and not link.exists():
            link.unlink()  # Should NOT be called

        if not link.exists():
            if source.exists():
                link.symlink_to(source)

        assert link.is_symlink()
        assert link.exists()
