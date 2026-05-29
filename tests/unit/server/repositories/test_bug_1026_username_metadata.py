"""
Test for Bug #1026: Missing 'username' field in single-repo activation metadata.

Root Cause: _do_activate_repository() built the metadata dict without the
'username' key, even though username is a parameter of the method. This caused
the activated-repo listing to return dicts without username, breaking any
caller that expects that field to be present.

Fix:
  1. metadata dict in _do_activate_repository() now includes "username": username.
  2. _list_user_repos_fs() calls repo_data.setdefault("username", username) to
     backfill the field for pre-existing metadata files that lack it.
"""

import json
import os
import shutil
import tempfile
from typing import Any, Dict
from unittest.mock import MagicMock, patch


from code_indexer.server.repositories.activated_repo_manager import ActivatedRepoManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(data_dir: str) -> ActivatedRepoManager:
    """Return a minimal ActivatedRepoManager backed by a temp filesystem dir."""
    golden_repo_manager = MagicMock()
    golden_repo_manager.golden_repos = {}
    background_job_manager = MagicMock()
    background_job_manager.submit_job.return_value = "job-test-1026"
    return ActivatedRepoManager(
        data_dir=data_dir,
        golden_repo_manager=golden_repo_manager,
        background_job_manager=background_job_manager,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBug1026UsernameMetadata:
    """Regression tests for Bug #1026 — username missing from activation metadata."""

    def setup_method(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.manager = _make_manager(self.temp_dir)

    def teardown_method(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Test 1: _do_activate_repository writes 'username' into metadata
    # ------------------------------------------------------------------

    def test_activate_metadata_contains_username(self) -> None:
        """
        _do_activate_repository() must include 'username' in the metadata dict
        passed to _save_metadata so callers can identify who owns the repo.
        """
        captured: Dict[str, Any] = {}

        def fake_save_metadata(username: str, user_alias: str, metadata: dict) -> None:
            captured["username_arg"] = username
            captured["metadata"] = dict(metadata)

        username = "alice"
        golden_repo_alias = "test-repo"
        branch_name = "main"
        user_alias = "my-repo"

        # Set up golden_repo_manager mock to return a valid golden repo path.
        # get_golden_repo() and get_actual_repo_path() are called by _do_activate_repository.
        golden_repo_mock = MagicMock()
        golden_repo_mock.clone_path = os.path.join(
            self.temp_dir, "golden", golden_repo_alias
        )
        golden_repo_mock.default_branch = (
            branch_name  # must match branch_name to skip git checkout
        )
        golden_repo_mock.repo_url = "https://example.com/repo.git"
        self.manager.golden_repo_manager.get_golden_repo.return_value = golden_repo_mock
        self.manager.golden_repo_manager.get_actual_repo_path.return_value = (
            os.path.join(self.temp_dir, "golden", golden_repo_alias)
        )

        # Patch _save_metadata to capture the call without touching the filesystem
        with patch.object(
            self.manager, "_save_metadata", side_effect=fake_save_metadata
        ):
            # Patch git/filesystem operations that would fail in a unit test.
            # Real method is _clone_with_copy_on_write (not _clone_from_golden).
            with patch.object(
                self.manager, "_clone_with_copy_on_write", return_value=True
            ):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(
                        returncode=0, stdout="", stderr=""
                    )
                    with patch(
                        "code_indexer.server.repositories.activated_repo_manager"
                        ".CommitterResolutionService"
                    ) as mock_crs_class:
                        mock_crs = MagicMock()
                        # resolve_committer_email returns (email, ssh_key_used) 2-tuple
                        mock_crs.resolve_committer_email.return_value = (
                            "alice@example.com",
                            None,
                        )
                        mock_crs_class.return_value = mock_crs

                        # Create a fake repo directory so the method doesn't fail
                        # when looking for the cloned path
                        repo_dir = os.path.join(
                            self.temp_dir, "activated-repos", username, user_alias
                        )
                        os.makedirs(repo_dir, exist_ok=True)

                        self.manager._do_activate_repository(
                            username=username,
                            golden_repo_alias=golden_repo_alias,
                            branch_name=branch_name,
                            user_alias=user_alias,
                        )

        assert "metadata" in captured, (
            "_save_metadata was never called — the method may have returned early"
        )
        assert "username" in captured["metadata"], (
            f"Bug #1026: 'username' missing from metadata dict. "
            f"Keys present: {list(captured['metadata'].keys())}"
        )
        assert captured["metadata"]["username"] == username, (
            f"Expected username='{username}', got '{captured['metadata']['username']}'"
        )

    # ------------------------------------------------------------------
    # Test 2: _list_user_repos_fs backfills missing 'username'
    # ------------------------------------------------------------------

    def test_list_user_repos_backfills_missing_username(self) -> None:
        """
        _list_user_repos_fs() must call repo_data.setdefault('username', username)
        so that pre-existing metadata files (written before the bug fix) get the
        field backfilled from the directory name.
        """
        username = "bob"
        user_alias = "old-repo"

        # Create the directory structure the real code expects:
        #   activated-repos/<username>/<user_alias>/   (repo clone dir)
        #   activated-repos/<username>/<user_alias>_metadata.json  (metadata file)
        user_dir = os.path.join(self.temp_dir, "activated-repos", username)
        repo_dir = os.path.join(user_dir, user_alias)
        os.makedirs(repo_dir, exist_ok=True)

        # Metadata file deliberately does NOT contain 'username' (simulates old data)
        metadata: Dict[str, Any] = {
            "user_alias": user_alias,
            "golden_repo_alias": "some-repo",
            "current_branch": "main",
            "activated_at": "2024-01-01T00:00:00+00:00",
            "last_accessed": "2024-01-01T00:00:00+00:00",
            "git_committer_email": "bob@example.com",
            "ssh_key_used": None,
        }
        metadata_path = os.path.join(user_dir, f"{user_alias}_metadata.json")
        with open(metadata_path, "w") as f:
            json.dump(metadata, f)

        result = self.manager._list_user_repos_fs(username)

        assert len(result) == 1, f"Expected 1 repo, got {len(result)}"
        repo_data = result[0]

        assert "username" in repo_data, (
            f"Bug #1026: 'username' was not backfilled by _list_user_repos_fs. "
            f"Keys present: {list(repo_data.keys())}"
        )
        assert repo_data["username"] == username, (
            f"Expected backfilled username='{username}', got '{repo_data['username']}'"
        )
