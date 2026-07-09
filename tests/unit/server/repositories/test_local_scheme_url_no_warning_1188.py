"""
Tests for Bug #1188: local:// scheme repos must NOT emit WARNING in canonical-URL match loop.

The canonical-URL match loop in GoldenRepoManager.find_by_canonical_url() and
ActivatedRepoManager.find_by_canonical_url() previously logged a WARNING when a
repo's URL used the internal local:// scheme, because git_url_normalizer correctly
raises GitUrlNormalizationError for non-git schemes.

Fix: skip local:// repos before attempting normalization (they can never match a
canonical git URL). The existing except-Exception must remain as a safety net for
genuinely malformed URLs.
"""

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
)
from code_indexer.server.repositories.activated_repo_manager import ActivatedRepoManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_golden_repo_manager(data_dir: str) -> GoldenRepoManager:
    """Create a GoldenRepoManager pointing at a temp data dir."""
    return GoldenRepoManager(data_dir=data_dir)


def _inject_golden_repo(mgr: GoldenRepoManager, alias: str, repo_url: str) -> None:
    """
    Inject a GoldenRepo into both the in-memory dict AND the SQLite backend so that
    both find_by_canonical_url (reads golden_repos dict) and get_golden_repo() (reads
    SQLite) can find it.
    """
    clone_path = os.path.join(mgr.golden_repos_dir, alias)
    created_at = datetime.now(timezone.utc).isoformat()
    mgr.golden_repos[alias] = GoldenRepo(
        alias=alias,
        repo_url=repo_url,
        default_branch="main",
        clone_path=clone_path,
        created_at=created_at,
    )
    mgr._sqlite_backend.add_repo(
        alias=alias,
        repo_url=repo_url,
        default_branch="main",
        clone_path=clone_path,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# GoldenRepoManager tests
# ---------------------------------------------------------------------------


class TestGoldenRepoManagerLocalSchemeNoWarning:
    """Bug #1188: local:// repos must not produce WARNING in find_by_canonical_url."""

    def setup_method(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(str(self.temp_dir), ignore_errors=True)

    def test_local_scheme_repo_does_not_emit_warning(self, caplog):
        """
        RED test: A golden repo with repo_url='local://<alias>' must NOT emit a WARNING
        containing 'Failed to normalize URL' when find_by_canonical_url is called.
        This test FAILS against the unfixed code (WARNING is logged) and passes after fix.
        """
        mgr = _make_golden_repo_manager(str(self.temp_dir))

        # Inject two local:// repos (e.g. cidx-meta, langfuse).
        # Search for a canonical URL that won't match any repo, so we never
        # reach get_actual_repo_path (which needs disk paths). The test is
        # purely about whether WARNING is logged for local:// during normalization.
        _inject_golden_repo(mgr, "cidx-meta", "local://cidx-meta")
        _inject_golden_repo(mgr, "langfuse", "local://langfuse")

        with caplog.at_level(logging.WARNING, logger="code_indexer"):
            mgr.find_by_canonical_url("github.com/example/no-match-here")

        warning_msgs = [
            r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING and "Failed to normalize URL" in r.message
        ]
        assert warning_msgs == [], (
            f"Expected no 'Failed to normalize URL' WARNING, but got: {warning_msgs}"
        )

    def test_local_scheme_repo_is_excluded_from_matches(self, caplog):
        """
        local:// repos must never appear in the match results (they have no canonical git URL).
        """
        mgr = _make_golden_repo_manager(str(self.temp_dir))
        _inject_golden_repo(mgr, "cidx-meta", "local://cidx-meta")

        with caplog.at_level(logging.DEBUG, logger="code_indexer"):
            results = mgr.find_by_canonical_url("github.com/example/anything")

        assert results == [], f"Expected empty results, got: {results}"

    def test_genuinely_malformed_url_still_warns(self, caplog):
        """
        Safety net: a genuinely malformed (non-local://) URL must still produce a WARNING,
        confirming that the except-Exception block was NOT removed by the fix.
        """
        mgr = _make_golden_repo_manager(str(self.temp_dir))
        _inject_golden_repo(mgr, "bad-repo", "not-a-valid-url-at-all")

        with caplog.at_level(logging.WARNING, logger="code_indexer"):
            results = mgr.find_by_canonical_url("github.com/example/anything")

        warning_msgs = [
            r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING and "Failed to normalize URL" in r.message
        ]
        assert results == []
        assert warning_msgs != [], (
            "Expected a WARNING for genuinely malformed URL but none was logged; "
            "the except-Exception safety net must remain intact."
        )


# ---------------------------------------------------------------------------
# ActivatedRepoManager tests
# ---------------------------------------------------------------------------


class TestActivatedRepoManagerLocalSchemeNoWarning:
    """Bug #1188: local:// repos must not produce WARNING in activated find_by_canonical_url."""

    def setup_method(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(str(self.temp_dir), ignore_errors=True)

    def _make_arm_with_local_repo(self) -> ActivatedRepoManager:
        """
        Build an ActivatedRepoManager whose golden_repo_manager has a local:// repo,
        with one fake user directory containing one activated-repo JSON pointing to it.
        """
        data_dir = str(self.temp_dir)

        golden_mgr = _make_golden_repo_manager(data_dir)
        _inject_golden_repo(golden_mgr, "cidx-meta", "local://cidx-meta")

        arm = ActivatedRepoManager(
            data_dir=data_dir,
            golden_repo_manager=golden_mgr,
        )

        # Create a fake activated-repo metadata file for user "testuser"
        user_dir = os.path.join(arm.activated_repos_dir, "testuser")
        os.makedirs(user_dir, exist_ok=True)

        meta = {
            "golden_repo_alias": "cidx-meta",
            "user_alias": "cidx-meta",
            "username": "testuser",
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "default_branch": "main",
        }
        meta_path = os.path.join(user_dir, "cidx-meta_metadata.json")
        with open(meta_path, "w") as f:
            json.dump(meta, f)

        # _list_user_repos_fs requires the repo directory (user_dir/user_alias) to
        # exist on disk before it returns the metadata entry. Create it so the
        # activated-repo metadata is actually returned by list_activated_repositories.
        repo_clone_dir = os.path.join(user_dir, "cidx-meta")
        os.makedirs(repo_clone_dir, exist_ok=True)

        return arm

    def test_local_scheme_activated_repo_does_not_emit_warning(self, caplog):
        """
        RED test: An activated repo backed by a golden repo with local:// URL must NOT emit
        a WARNING containing 'Failed to normalize URL for activated repo' when
        find_by_canonical_url is called.
        This test FAILS against the unfixed code (WARNING is logged) and passes after fix.
        """
        arm = self._make_arm_with_local_repo()

        with caplog.at_level(logging.WARNING, logger="code_indexer"):
            arm.find_by_canonical_url("github.com/example/anything")

        warning_msgs = [
            r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING
            and "Failed to normalize URL for activated repo" in r.message
        ]
        assert warning_msgs == [], (
            f"Expected no WARNING for activated repo local:// URL, but got: {warning_msgs}"
        )

    def test_local_scheme_activated_repo_excluded_from_matches(self, caplog):
        """
        local:// backed activated repos must never appear in match results.
        """
        arm = self._make_arm_with_local_repo()

        with caplog.at_level(logging.DEBUG, logger="code_indexer"):
            results = arm.find_by_canonical_url("github.com/example/anything")

        assert results == [], f"Expected empty results, got: {results}"
