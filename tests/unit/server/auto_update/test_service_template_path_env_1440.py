"""Contract test for Bug #1440: the cidx-auto-update.service template must
declare a PATH= environment line that includes {HOME}/.local/bin.

Prior to this fix the template had NO Environment="PATH=..." line at all.
On hosts where `cidx` is installed to a per-user location (e.g.
~/.local/bin, not on systemd's minimal compiled-in default PATH), running
under the cidx-auto-update.service unit, shutil.which("cidx") in
DeploymentExecutor._get_cli_python_interpreter() correctly returns None --
but for the WRONG reason (a PATH gap, not "cidx genuinely not installed"),
silently no-op'ing the Bug #1392 CLI/hnswlib sync mechanism. Confirmed via
journalctl on all 3 staging nodes: every run across a full week of history
logs the "nothing to sync yet" skip, never the actual sync.

This is a template-only fix for FUTURE installs -- it does not touch
_get_cli_python_interpreter()'s own dynamic shutil.which + shebang-parsing
discovery, which is correct and unchanged (see
test_deployment_executor_hnswlib_cli_sync_1392.py).

Mirrors the existing test_service_template_branch_env.py contract-test
pattern exactly (static content assertions against the template file).
"""

from pathlib import Path

import pytest

# This test file lives at tests/unit/server/auto_update/<file>.py -- four
# parents up from __file__ is the repository root (mirrors the identical
# depth used by the sibling test_service_template_branch_env.py contract
# test in this same directory).
_REPO_ROOT = Path(__file__).resolve().parents[4]

TEMPLATE_PATH = (
    _REPO_ROOT
    / "src"
    / "code_indexer"
    / "server"
    / "auto_update"
    / "templates"
    / "cidx-auto-update.service"
)

EXPECTED_PATH_LINE = (
    'Environment="PATH={HOME}/.local/bin:'
    '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"'
)


class TestServiceTemplatePathEnv:
    """Test suite for the {HOME}-parameterized PATH= line in the template."""

    def test_template_declares_path_environment_placeholder(self):
        """Template must contain a PATH= Environment line with .local/bin."""
        content = TEMPLATE_PATH.read_text()
        assert EXPECTED_PATH_LINE in content, (
            f"Expected line not found in template:\n{EXPECTED_PATH_LINE!r}\n"
            f"Template content:\n{content}"
        )

    @pytest.mark.parametrize(
        "expected_fragment",
        [
            "User={USER}",
            'Environment="CIDX_SERVER_REPO_PATH={REPO_PATH}"',
            'Environment="CIDX_AUTO_UPDATE_BRANCH={BRANCH}"',
        ],
    )
    def test_template_still_declares_pre_existing_placeholders(
        self, expected_fragment: str
    ):
        """Pre-existing User/REPO_PATH/BRANCH placeholder lines must remain
        untouched by this change."""
        content = TEMPLATE_PATH.read_text()
        assert expected_fragment in content
