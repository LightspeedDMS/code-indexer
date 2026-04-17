"""
Unit tests for Story #738: Research Assistant — Grant Remediation Authority.

Tests verify that _build_permission_settings constructs the correct
permission_settings dict after the Story #738 changes:

  - Removed deny rules are absent (destructive FS, process control, curl,
    and broad systemctl restart *)
  - Retained hard-deny rules are all present
  - New specific allow for systemctl restart cidx-server is present
  - Scoped Write/Edit cidx-meta allow rules are unchanged (Story #554)
  - Tool-level WebFetch/WebSearch deny rules are unchanged
  - cidx-meta-cleanup.sh allow rule is present when cidx_repo_root resolves

No mocks of the service itself.  Tests use the real code path:
  ResearchAssistantService._build_permission_settings().
"""

import pytest


# ---------------------------------------------------------------------------
# Rules that must be REMOVED from deny (remediation authority granted #738)
# ---------------------------------------------------------------------------
REMOVED_DENY_RULES = [
    # Destructive filesystem ops — now permitted for remediation
    "Bash(rm *)",
    "Bash(mv *)",
    "Bash(cp *)",
    "Bash(mkdir *)",
    "Bash(rmdir *)",
    "Bash(touch *)",
    "Bash(chmod *)",
    "Bash(chown *)",
    "Bash(ln *)",
    # Process control — unblocked for SIGTERM on stuck processes
    "Bash(kill *)",
    "Bash(pkill *)",
    # Network — localhost curl allowed for admin API diagnostics
    "Bash(curl *)",
    # Blanket systemctl restart * — replaced by a specific allow rule
    "Bash(systemctl restart *)",
]

# ---------------------------------------------------------------------------
# Hard-deny rules that must REMAIN in deny after #738
# ---------------------------------------------------------------------------
RETAINED_DENY_RULES = [
    # Privilege escalation
    "Bash(sudo *)",
    "Bash(su *)",
    "Bash(doas *)",
    # Network exfiltration / lateral movement
    "Bash(ssh *)",
    "Bash(scp *)",
    "Bash(rsync *)",
    "Bash(wget *)",
    "Bash(telnet *)",
    "Bash(ftp *)",
    "Bash(sftp *)",
    "Bash(nc *)",
    "Bash(ncat *)",
    "Bash(nmap *)",
    "Bash(netcat *)",
    "Bash(socat *)",
    # Scripting interpreters — arbitrary code execution
    "Bash(python3 *)",
    "Bash(python *)",
    "Bash(perl *)",
    "Bash(ruby *)",
    "Bash(node *)",
    "Bash(php *)",
    "Bash(lua *)",
    # Shell escape hatches
    "Bash(bash *)",
    "Bash(sh *)",
    "Bash(zsh *)",
    "Bash(exec *)",
    "Bash(eval *)",
    # Command multipliers
    "Bash(find *)",
    "Bash(xargs *)",
    # Package management
    "Bash(apt *)",
    "Bash(apt-get *)",
    "Bash(dnf *)",
    "Bash(yum *)",
    "Bash(pip *)",
    "Bash(pip3 *)",
    "Bash(npm *)",
    "Bash(gem *)",
    # Disk/mount
    "Bash(mkfs *)",
    "Bash(fdisk *)",
    "Bash(mount *)",
    "Bash(umount *)",
    # Git write operations
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(git checkout *)",
    "Bash(git reset *)",
    "Bash(git rebase *)",
    "Bash(git merge *)",
    "Bash(git stash *)",
    "Bash(git clean *)",
    "Bash(git restore *)",
    # Cron / scheduling (persistence)
    "Bash(crontab *)",
    "Bash(at *)",
    # Redirection exfiltration
    "Bash(tee *)",
    # Process control: killall stays denied; kill/pkill are unblocked
    "Bash(killall *)",
    # Service management: all systemctl sub-commands except restart cidx-server
    "Bash(systemctl stop *)",
    "Bash(systemctl start *)",
    "Bash(systemctl enable *)",
    "Bash(systemctl disable *)",
    "Bash(systemctl reload *)",
]

# Tool-level deny rules that must remain
TOOL_LEVEL_DENY_RULES = ["Write", "Edit", "WebFetch", "WebSearch"]


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
def cleanup_script_rule(tmp_path):
    script = str(tmp_path / "scripts" / "cidx-meta-cleanup.sh")
    return f"Bash({script} *)"


@pytest.fixture
def permission_settings(service, cidx_meta_path, cleanup_script_rule):
    """Build permission_settings via the real helper method."""
    return service._build_permission_settings(cidx_meta_path, cleanup_script_rule)


@pytest.fixture
def deny_list(permission_settings):
    return permission_settings["permissions"]["deny"]


@pytest.fixture
def allow_list(permission_settings):
    return permission_settings["permissions"]["allow"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPermissionSettingsStructure:
    """Top-level shape of the returned dict."""

    def test_has_permissions_key(self, permission_settings):
        assert "permissions" in permission_settings

    def test_permissions_has_allow_and_deny(self, permission_settings):
        perms = permission_settings["permissions"]
        assert "allow" in perms
        assert "deny" in perms

    def test_allow_and_deny_are_lists(self, permission_settings):
        perms = permission_settings["permissions"]
        assert isinstance(perms["allow"], list)
        assert isinstance(perms["deny"], list)


class TestRemovedDenyRules:
    """Rules that must NOT appear in deny after Story #738."""

    @pytest.mark.parametrize("rule", REMOVED_DENY_RULES)
    def test_rule_not_in_deny(self, deny_list, rule):
        assert rule not in deny_list, (
            f"{rule!r} must be removed from deny list (Story #738). "
            f"Still found in: {deny_list}"
        )


class TestRetainedHardDenyRules:
    """Hard-deny rules that must remain in deny even after #738 loosening."""

    @pytest.mark.parametrize("rule", RETAINED_DENY_RULES)
    def test_rule_still_in_deny(self, deny_list, rule):
        assert rule in deny_list, (
            f"{rule!r} must remain in deny list (Story #738 retained). "
            f"Got deny list: {deny_list}"
        )


class TestToolLevelDenyRules:
    """Tool-level deny rules must remain intact."""

    @pytest.mark.parametrize("rule", TOOL_LEVEL_DENY_RULES)
    def test_tool_level_rule_in_deny(self, deny_list, rule):
        assert rule in deny_list, (
            f"Tool-level deny {rule!r} must remain. Got deny: {deny_list}"
        )


class TestNewAllowRules:
    """New allow rules added by Story #738."""

    def test_systemctl_restart_cidx_server_in_allow(self, allow_list):
        """The one specific systemctl restart rule must be explicitly allowed."""
        assert "Bash(systemctl restart cidx-server)" in allow_list, (
            "Bash(systemctl restart cidx-server) must be in allow list. "
            f"Got: {allow_list}"
        )


class TestExistingAllowRulesPreserved:
    """Existing allow rules from Story #554 must remain unchanged."""

    def test_cidx_meta_write_in_allow(self, allow_list):
        write_rules = [r for r in allow_list if "Write(" in r and "cidx-meta" in r]
        assert len(write_rules) >= 1, (
            f"Write scoped to cidx-meta must be in allow. Got: {allow_list}"
        )

    def test_cidx_meta_edit_in_allow(self, allow_list):
        edit_rules = [r for r in allow_list if "Edit(" in r and "cidx-meta" in r]
        assert len(edit_rules) >= 1, (
            f"Edit scoped to cidx-meta must be in allow. Got: {allow_list}"
        )

    def test_cleanup_script_rule_in_allow_when_provided(self, allow_list):
        cleanup_rules = [r for r in allow_list if "cidx-meta-cleanup.sh" in r]
        assert len(cleanup_rules) >= 1, (
            f"Cleanup script rule must be in allow when provided. Got: {allow_list}"
        )

    def test_read_glob_grep_todowrite_in_allow(self, allow_list):
        for tool in ("Read", "Glob", "Grep", "TodoWrite"):
            assert tool in allow_list, (
                f"'{tool}' must be in allow list. Got: {allow_list}"
            )


class TestOptionalCleanupRule:
    """Cleanup rule is omitted when None is passed."""

    def test_no_cleanup_rule_when_none(self, service, cidx_meta_path):
        settings = service._build_permission_settings(cidx_meta_path, None)
        allow_list = settings["permissions"]["allow"]
        cleanup_rules = [r for r in allow_list if "cidx-meta-cleanup.sh" in r]
        assert cleanup_rules == [], (
            f"No cleanup rule expected when None passed. Got: {allow_list}"
        )
