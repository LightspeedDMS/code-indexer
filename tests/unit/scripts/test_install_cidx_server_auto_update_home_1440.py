"""Contract test for Bug #1440: scripts/install-cidx-server.sh's
install_auto_update_service() function must substitute the new {HOME}
placeholder introduced into the cidx-auto-update.service template's
Environment="PATH={HOME}/.local/bin:..." line.

Prior to this fix the function substituted only {USER}, {REPO_PATH}, and
{BRANCH} -- a fresh install via this script would leave a literal "{HOME}"
in the deployed unit's PATH, which is exactly as broken as having no PATH
line at all (systemd would treat the whole value as one bogus PATH
component, never resolving ~/.local/bin).

This is a static content assertion against the script source (the
established pattern for bash-generated systemd unit content in this
codebase -- see test_service_template_branch_env.py's equivalent check
against the Python-templated unit).
"""

from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "install-cidx-server.sh"


def _install_auto_update_service_body() -> str:
    """Return the install_auto_update_service() function body (a generous
    slice bounded by its start marker -- sufficient to see its substitution
    lines without needing full bash-parsing)."""
    content = SCRIPT_PATH.read_text()
    func_start = content.index("install_auto_update_service()")
    return content[func_start : func_start + 3000]


class TestInstallScriptAutoUpdateHomeSubstitution:
    """Test suite for the {HOME} substitution in install_auto_update_service()."""

    def test_install_auto_update_service_substitutes_home_placeholder(self):
        """The function must bash-substitute {HOME} using the script's own
        $HOME, mirroring the existing {USER}/{REPO_PATH}/{BRANCH}
        substitution lines exactly."""
        func_body = _install_auto_update_service_body()

        assert r'unit_content="${unit_content//\{HOME\}/${HOME}}"' in func_body, (
            "Expected a {HOME} -> ${HOME} substitution line inside "
            f"install_auto_update_service():\n{func_body}"
        )
