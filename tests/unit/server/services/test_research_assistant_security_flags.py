"""
Unit tests for Story #554: Research Assistant Security Hardening - CLI Flags.

Tests verify that _run_claude_background builds the claude CLI command with
the required security flags: --tools, --disallowedTools, --settings.

Acceptance Criteria covered:
- AC1: Bash allowlist enforced via --settings permissions.allow
- AC2: Safe Bash commands allowed (sqlite3, journalctl, ls, grep, etc.)
- AC3: Write/Edit scoped to cidx-meta via --settings permissions
- AC5: Blocked tools via --disallowedTools flag
- AC8: Existing session/resume logic preserved

Following TDD methodology: Tests written FIRST before implementing.
"""

import json
import threading
import tempfile
import shutil
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Named constant for background thread synchronization timeout
BACKGROUND_THREAD_WAIT_SECONDS = 5.0


@pytest.mark.slow
class TestSecurityHardeningCommandFlags:
    """
    Tests for AC1/AC2/AC3/AC5: _run_claude_background must include
    security flags in the subprocess command.
    """

    @pytest.fixture
    def temp_db(self):
        """Create temporary database for testing."""
        import os

        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test.db")
        yield db_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def research_service(self, temp_db):
        """Create ResearchAssistantService with temporary database."""
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )

        schema = DatabaseSchema(db_path=temp_db)
        schema.initialize_database()
        return ResearchAssistantService(db_path=temp_db)

    def _run_and_capture_calls(self, research_service, message, is_subsequent=False):
        """
        Execute a prompt through the service and capture all subprocess.run calls.

        Uses a threading.Event for synchronization to avoid flaky time.sleep waits.
        Returns list of captured cmd lists.
        """
        session = research_service.create_session()
        session_id = session["id"]

        if is_subsequent:
            research_service.add_message(session_id, "user", "Prior question")
            research_service.add_message(session_id, "assistant", "Prior answer")

        captured_calls = []
        done_event = threading.Event()

        def capture_run(cmd, **kwargs):
            captured_calls.append(list(cmd))
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "Claude response"
            mock_result.stderr = ""
            done_event.set()
            return mock_result

        target = (
            "code_indexer.server.services.research_assistant_service.subprocess.run"
        )
        with patch(target, side_effect=capture_run):
            research_service.execute_prompt(session_id, message)
            done_event.wait(timeout=BACKGROUND_THREAD_WAIT_SECONDS)

        return captured_calls

    def _get_settings(self, cmd):
        """Extract and parse the --settings JSON from a command list."""
        settings_idx = cmd.index("--settings")
        return json.loads(cmd[settings_idx + 1])

    # ------------------------------------------------------------------
    # AC5: --disallowedTools flag
    # ------------------------------------------------------------------

    def test_command_includes_disallowed_tools_flag(self, research_service):
        """AC5: Command must include --disallowedTools to block dangerous tools."""
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1, "subprocess.run must have been called"

        assert "--disallowedTools" in calls[0], (
            f"Command must include --disallowedTools flag. Got cmd: {calls[0]}"
        )

    def test_disallowed_tools_blocks_all_required_tools(self, research_service):
        """
        AC5: WebFetch, WebSearch, Agent, Skill, NotebookEdit must all be
        in the --disallowedTools value.
        """
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1

        disallowed_idx = calls[0].index("--disallowedTools")
        disallowed_value = calls[0][disallowed_idx + 1]

        for tool in ("WebFetch", "WebSearch", "Agent", "Skill", "NotebookEdit"):
            assert tool in disallowed_value, (
                f"{tool} must be disallowed. Got disallowedTools: {disallowed_value!r}"
            )

    # ------------------------------------------------------------------
    # AC5: --tools allowlist flag
    # ------------------------------------------------------------------

    def test_command_includes_tools_flag(self, research_service):
        """AC5: Command must include --tools to restrict available tool set."""
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1

        assert "--tools" in calls[0], (
            f"Command must include --tools flag. Got cmd: {calls[0]}"
        )

    def test_tools_flag_includes_required_tools(self, research_service):
        """AC2/AC3: --tools must include Bash, Read, Write, Edit, Glob, Grep, TodoWrite."""
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1

        tools_idx = calls[0].index("--tools")
        tools_value = calls[0][tools_idx + 1]

        for tool in ("Bash", "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite"):
            assert tool in tools_value, (
                f"{tool} must be in --tools value. Got: {tools_value!r}"
            )

    # ------------------------------------------------------------------
    # AC1/AC2/AC3: --settings permission JSON structure
    # ------------------------------------------------------------------

    def test_command_includes_settings_flag(self, research_service):
        """AC1/AC2/AC3: Command must include --settings flag with permission JSON."""
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1

        assert "--settings" in calls[0], (
            f"Command must include --settings flag. Got cmd: {calls[0]}"
        )

    def test_settings_is_valid_json_with_permissions(self, research_service):
        """AC1: --settings value must be valid JSON with permissions.allow and .deny."""
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1

        settings = self._get_settings(calls[0])

        assert "permissions" in settings, (
            f"Settings must have 'permissions' key. Got keys: {list(settings.keys())}"
        )
        perms = settings["permissions"]
        assert "allow" in perms, (
            f"permissions must have 'allow'. Got: {list(perms.keys())}"
        )
        assert "deny" in perms, (
            f"permissions must have 'deny'. Got: {list(perms.keys())}"
        )

    def test_settings_deny_blocks_write_edit_webfetch_websearch(self, research_service):
        """
        AC3/AC5: permissions.deny must include Write, Edit, WebFetch, WebSearch.
        Specific allow rules for cidx-meta take precedence over these general denies.
        """
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1

        deny_list = self._get_settings(calls[0])["permissions"]["deny"]

        for entry in ("Write", "Edit", "WebFetch", "WebSearch"):
            assert entry in deny_list, (
                f"'{entry}' must be in deny list. Got deny: {deny_list}"
            )

    def test_settings_allow_includes_cidx_meta_write_and_edit(self, research_service):
        """
        AC3: permissions.allow must contain Write and Edit rules scoped to cidx-meta,
        overriding the general deny rules for those paths.
        """
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1

        allow_list = self._get_settings(calls[0])["permissions"]["allow"]

        write_rules = [r for r in allow_list if "Write" in r and "cidx-meta" in r]
        assert len(write_rules) >= 1, (
            f"permissions.allow must contain Write rule scoped to cidx-meta. "
            f"Got allow list: {allow_list}"
        )

        edit_rules = [r for r in allow_list if "Edit" in r and "cidx-meta" in r]
        assert len(edit_rules) >= 1, (
            f"permissions.allow must contain Edit rule scoped to cidx-meta. "
            f"Got allow list: {allow_list}"
        )

    def test_settings_deny_blocks_dangerous_bash_commands(self, research_service):
        """
        AC1: permissions.deny must include deny rules for all dangerous Bash commands.
        With --dangerously-skip-permissions, only deny rules actually block execution.
        Allow rules only control prompting (useless with skip-permissions).
        Categories: network (curl, wget, ssh, scp, nc, nmap), interpreters (python3,
        python, perl, ruby, node), shell escapes (bash, sh, xargs, find), privilege
        (sudo), destructive (rm, mv, cp, chmod), packages (apt, pip), service mgmt
        (systemctl restart/stop/start), git writes (push/commit/checkout), process
        control (kill), exfiltration (tee), persistence (crontab).
        """
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1

        deny_list = self._get_settings(calls[0])["permissions"]["deny"]
        bash_deny_rules = [r for r in deny_list if r.startswith("Bash(")]

        required_denies = [
            "curl",
            "wget",
            "ssh",
            "scp",
            "python3",
            "python ",
            "perl",
            "ruby",
            "node",
            "sudo",
            "rm ",
            "mv ",
            "cp ",
            "chmod",
            "xargs",
            "find",
            "bash ",
            "sh ",
            "nc ",
            "nmap",
            "apt ",
            "pip ",
            "systemctl restart",
            "systemctl stop",
            "systemctl start",
            "git push",
            "git commit",
            "git checkout",
            "kill ",
            "tee ",
            "crontab",
        ]

        for pattern in required_denies:
            matching = [r for r in bash_deny_rules if pattern in r]
            assert len(matching) >= 1, (
                f"Bash deny rule for {pattern!r} must be in deny list. "
                f"With --dangerously-skip-permissions, only deny rules block execution. "
                f"Got bash deny rules: {bash_deny_rules}"
            )

    def test_cleanup_script_uses_fully_qualified_path(self, research_service):
        """
        HIGH-2: The cidx-meta-cleanup.sh rule must use a fully-qualified path,
        not a bare script name. Bare script names require PATH configuration
        and are less secure than absolute paths.
        """
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1

        allow_list = self._get_settings(calls[0])["permissions"]["allow"]
        cleanup_rules = [r for r in allow_list if "cidx-meta-cleanup.sh" in r]
        assert len(cleanup_rules) >= 1, (
            "Allow list must contain a cidx-meta-cleanup.sh rule. "
            f"Got allow list: {allow_list}"
        )
        # The rule must use an absolute path: Bash(/absolute/path/cidx-meta-cleanup.sh *)
        for rule in cleanup_rules:
            assert rule.startswith("Bash(/"), (
                f"HIGH-2: cidx-meta-cleanup.sh rule must use fully-qualified absolute path. "
                f"Got: {rule!r}. Expected: Bash(/path/to/scripts/cidx-meta-cleanup.sh *)"
            )

    def test_cidx_meta_base_in_subprocess_env(self, research_service):
        """
        MEDIUM-1: CIDX_META_BASE must be set in the subprocess environment
        so that cidx-meta-cleanup.sh knows its base directory.
        """
        session = research_service.create_session()
        session_id = session["id"]

        captured_envs = []
        done_event = threading.Event()

        def capture_run(cmd, **kwargs):
            captured_envs.append(kwargs.get("env", {}))
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "Claude response"
            mock_result.stderr = ""
            done_event.set()
            return mock_result

        target = (
            "code_indexer.server.services.research_assistant_service.subprocess.run"
        )
        with patch(target, side_effect=capture_run):
            research_service.execute_prompt(session_id, "Test question")
            done_event.wait(timeout=BACKGROUND_THREAD_WAIT_SECONDS)

        assert len(captured_envs) >= 1, "subprocess.run must have been called"
        env = captured_envs[0]
        assert "CIDX_META_BASE" in env, (
            "MEDIUM-1: CIDX_META_BASE must be set in the subprocess environment "
            "so cidx-meta-cleanup.sh can locate its base directory. "
            f"Got env keys containing CIDX: {[k for k in env if 'CIDX' in k]}"
        )

    def test_pipe_behavior_comment_present(self):
        """
        HIGH-3: The service source must contain a comment documenting that
        Claude Code's Bash rules are shell-operator-aware (pipe/&&/|| behavior).
        This documents the security assumption for audit trail purposes.
        """
        service_path = (
            Path(__file__).parents[4]
            / "src"
            / "code_indexer"
            / "server"
            / "services"
            / "research_assistant_service.py"
        )
        assert service_path.exists(), f"Service file must exist at {service_path}"
        content = service_path.read_text()
        # The comment must document shell-operator-aware behavior of Claude Code Bash rules
        has_comment = any(
            phrase in content
            for phrase in (
                "shell-operator-aware",
                "shell operator-aware",
                "pipe operator",
                "Bash rules are shell",
            )
        )
        assert has_comment, (
            "HIGH-3: research_assistant_service.py must contain a comment above the "
            "Bash allowlist documenting that Claude Code Bash rules are shell-operator-aware "
            "(blocks cmd && blocked, cmd | blocked). Required for security audit trail."
        )

    def test_settings_allow_includes_read_glob_grep_unscoped(self, research_service):
        """
        AC2/AC8: permissions.allow must include unscoped Read, Glob, Grep, TodoWrite
        so the assistant can investigate any file.
        """
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1

        allow_list = self._get_settings(calls[0])["permissions"]["allow"]

        for tool in ("Read", "Glob", "Grep", "TodoWrite"):
            assert tool in allow_list, (
                f"'{tool}' must be in allow list (unscoped). Got: {allow_list}"
            )

    # ------------------------------------------------------------------
    # AC8: Existing session logic preserved
    # ------------------------------------------------------------------

    def test_session_id_flag_present_for_first_message(self, research_service):
        """AC8: --session-id must still be present for first messages."""
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1

        assert "--session-id" in calls[0], (
            f"--session-id must still be in first-message command. Got: {calls[0]}"
        )

    def test_resume_flag_present_for_subsequent_message(self, research_service):
        """AC8: --resume must still be present for subsequent messages."""
        calls = self._run_and_capture_calls(
            research_service, "Follow-up", is_subsequent=True
        )
        assert len(calls) >= 1

        assert "--resume" in calls[0], (
            f"--resume must still be in subsequent-message command. Got: {calls[0]}"
        )

    def test_model_and_skip_permissions_still_present(self, research_service):
        """AC8: --model and --dangerously-skip-permissions must still be present."""
        calls = self._run_and_capture_calls(research_service, "Test question")
        assert len(calls) >= 1
        cmd = calls[0]

        assert "--model" in cmd, f"--model must still be in command. Got: {cmd}"
        assert "--dangerously-skip-permissions" in cmd, (
            f"--dangerously-skip-permissions must still be in command. Got: {cmd}"
        )

    def test_security_flags_present_on_subsequent_message(self, research_service):
        """AC1/AC5: Security flags must be present for subsequent messages too."""
        calls = self._run_and_capture_calls(
            research_service, "Follow-up", is_subsequent=True
        )
        assert len(calls) >= 1

        for flag in ("--tools", "--disallowedTools", "--settings"):
            assert flag in calls[0], (
                f"{flag} must be present in subsequent-message command. Got: {calls[0]}"
            )
