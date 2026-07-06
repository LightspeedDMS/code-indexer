"""
Contract test: the cidx-auto-update.service template must parameterize the
tracked branch via CIDX_AUTO_UPDATE_BRANCH, and run_once.py must read that
exact env var name.

Prior to this fix the template only set CIDX_SERVER_REPO_PATH, so a
freshly-installed auto-update unit had no CIDX_AUTO_UPDATE_BRANCH set at
all, and run_once.py silently defaulted to tracking "master" regardless of
which branch the operator actually intended (e.g. a staging node tracking
development). Verified against git history: the pre-fix committed template
contained zero occurrences of "CIDX_AUTO_UPDATE_BRANCH".
"""

from pathlib import Path

TEMPLATE_PATH = (
    Path(__file__).parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "auto_update"
    / "templates"
    / "cidx-auto-update.service"
)

RUN_ONCE_PATH = (
    Path(__file__).parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "auto_update"
    / "run_once.py"
)


class TestServiceTemplateBranchEnv:
    """Test suite for the {BRANCH}-parameterized auto-update service template."""

    def test_template_declares_branch_environment_placeholder(self):
        """Template must contain a CIDX_AUTO_UPDATE_BRANCH={BRANCH} Environment line."""
        content = TEMPLATE_PATH.read_text()
        assert 'Environment="CIDX_AUTO_UPDATE_BRANCH={BRANCH}"' in content

    def test_template_still_declares_repo_path_placeholder(self):
        """Pre-existing CIDX_SERVER_REPO_PATH line must remain untouched."""
        content = TEMPLATE_PATH.read_text()
        assert 'Environment="CIDX_SERVER_REPO_PATH={REPO_PATH}"' in content

    def test_run_once_reads_matching_env_var_name_with_master_default(self):
        """run_once.py's env-var contract must match the template's placeholder name."""
        content = RUN_ONCE_PATH.read_text()
        assert 'os.environ.get("CIDX_AUTO_UPDATE_BRANCH") or "master"' in content, (
            "run_once.py env-var contract must not change as part of this fix"
        )
