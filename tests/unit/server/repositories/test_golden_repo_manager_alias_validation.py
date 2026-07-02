"""
Regression tests for Bug #741 — alias ending in '-global' produces phantom '-global-global'.

When a golden repo is registered with an alias already ending in '-global'
(e.g. 'r53-global'), the global-activation step must NOT append '-global' again.

The chosen fix (per bug spec Part A) is to REJECT aliases ending in '-global'
at registration time with a clear ValueError, since '-global' is a reserved
internal suffix.

The '-global' suffix validation is synchronous and fires before any background
job is submitted, so it is testable without running the full clone workflow.

For the acceptance path (valid aliases), we use 'local://<alias>' URLs which
skip git network validation (Story #538) and reach the background-job submission
stage, returning a non-empty job_id string.
"""

import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


class TestGoldenRepoManagerAliasGlobalSuffixValidation:
    """Regression tests for Bug #741 — '-global' suffix validation in add_golden_repo."""

    def setup_method(self):
        """Set up a minimal GoldenRepoManager with a temp directory."""
        self.temp_dirs: list = []
        base_dir = Path(tempfile.mkdtemp())
        self.temp_dirs.append(base_dir)
        golden_repos_dir = base_dir / "golden_repos"
        golden_repos_dir.mkdir()
        self.manager = GoldenRepoManager(str(golden_repos_dir))

        # Wire background_job_manager mock so acceptance tests can reach
        # background-job submission (same pattern as test_golden_repo_manager_locking.py).
        def _mock_submit_job(
            operation_type, func, submitter_username, is_admin, repo_alias
        ):
            return f"test-job-{repo_alias}"

        self.manager.background_job_manager = Mock()
        self.manager.background_job_manager.submit_job.side_effect = _mock_submit_job

    def teardown_method(self):
        """Clean up temp directories."""
        for temp_dir in self.temp_dirs:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

    # ------------------------------------------------------------------
    # Bug #741 regression: reject alias ending in '-global'
    # ------------------------------------------------------------------

    def test_add_golden_repo_rejects_alias_ending_in_global(self):
        """
        Bug #741: add_golden_repo must raise ValueError when the user-supplied
        alias already ends with '-global', before any background job is submitted.

        Without the fix, registration would succeed and global activation would
        produce the phantom alias 'r53-global-global'.
        """
        with pytest.raises(ValueError, match="-global"):
            self.manager.add_golden_repo(
                repo_url="git@gitlab.com:example/r53-global.git",
                alias="r53-global",
            )

    def test_add_golden_repo_rejects_alias_with_just_global_suffix_token(self):
        """
        Edge case: the alias '-global' (only the reserved suffix token) must be
        rejected because it ends with '-global'.
        """
        with pytest.raises(ValueError, match="-global"):
            self.manager.add_golden_repo(
                repo_url="git@gitlab.com:example/repo.git",
                alias="-global",
            )

    def test_add_golden_repo_rejects_any_alias_ending_in_global(self):
        """
        Edge case: alias 'my-repo-global' must also be rejected — the '-global'
        suffix rule applies regardless of what precedes it.
        """
        with pytest.raises(ValueError, match="-global"):
            self.manager.add_golden_repo(
                repo_url="git@gitlab.com:example/my-repo-global.git",
                alias="my-repo-global",
            )

    # ------------------------------------------------------------------
    # Acceptance path: valid aliases must NOT be rejected by the new rule
    # ------------------------------------------------------------------

    def test_add_golden_repo_accepts_alias_with_global_in_middle(self):
        """
        An alias containing 'global' in the middle (e.g. 'my-global-repo') is
        valid and must NOT be rejected by the '-global' suffix rule.

        We use a 'local://' URL so that git network validation is skipped
        (Story #538) and add_golden_repo reaches background-job submission,
        returning a non-empty job_id string.
        """
        job_id = self.manager.add_golden_repo(
            repo_url="local://my-global-repo",
            alias="my-global-repo",
        )
        assert isinstance(job_id, str)
        assert len(job_id) > 0

    def test_add_golden_repo_accepts_normal_alias(self):
        """
        Sanity check: a normal alias (not ending in '-global') must pass
        the new validation and reach background-job submission.

        We use a 'local://' URL so that git network validation is skipped
        (Story #538) and add_golden_repo reaches background-job submission,
        returning a non-empty job_id string.
        """
        job_id = self.manager.add_golden_repo(
            repo_url="local://my-repo",
            alias="my-repo",
        )
        assert isinstance(job_id, str)
        assert len(job_id) > 0
