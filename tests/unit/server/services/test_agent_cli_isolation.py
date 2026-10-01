"""
Tests for agent_cli_isolation: shared isolation-policy helpers used by every
server-side Claude/Codex CLI invocation that analyzes a golden repository.

These are pure functions (no subprocess calls) so they are tested directly,
without mocking.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# is_isolation_exempt_flow — the narrow, explicit exemption list
# ---------------------------------------------------------------------------


class TestIsolationExemptFlow:
    def test_self_monitoring_scan_is_exempt(self):
        """self_monitoring_scan queries the server's own log database via
        Bash/sqlite3 and keeps its pre-existing invocation shape."""
        from code_indexer.server.services.agent_cli_isolation import (
            is_isolation_exempt_flow,
        )

        assert is_isolation_exempt_flow("self_monitoring_scan") is True

    def test_golden_repo_flows_are_not_exempt(self):
        from code_indexer.server.services.agent_cli_isolation import (
            is_isolation_exempt_flow,
        )

        for flow in (
            "repo_lifecycle",
            "dependency_map_pass_1",
            "dependency_map_pass_2",
            "dependency_map_delta_merge",
            "dependency_map_refinement",
            "dependency_map_verification",
            "dependency_map_new_domain",
            "dependency_map_domain_discovery",
        ):
            assert is_isolation_exempt_flow(flow) is False, flow


# ---------------------------------------------------------------------------
# Stable per-target neutral cwd (fleet-scale follow-up to the neutral-cwd
# isolation fix): a unique cwd per CLI call means the claude CLI's own
# per-cwd session-transcript folder under ~/.claude/projects/ grows one new
# folder PER CALL instead of one per repo -- unbounded at ~900 repos. The
# fix returns the SAME directory for the SAME target across calls, restoring
# the pre-existing one-folder-per-repo transcript behaviour.
# ---------------------------------------------------------------------------


class TestPrepareStableNeutralCwd:
    def test_same_target_gives_same_cwd_across_calls(self):
        from code_indexer.server.services.agent_cli_isolation import (
            prepare_stable_neutral_cwd,
        )

        target = "/data/golden-repos/example-repo"
        first = prepare_stable_neutral_cwd(target)
        second = prepare_stable_neutral_cwd(target)
        try:
            assert first == second
        finally:
            shutil.rmtree(first, ignore_errors=True)

    def test_different_targets_give_different_cwds(self):
        from code_indexer.server.services.agent_cli_isolation import (
            prepare_stable_neutral_cwd,
        )

        one = prepare_stable_neutral_cwd("/data/golden-repos/repo-one")
        two = prepare_stable_neutral_cwd("/data/golden-repos/repo-two")
        try:
            assert one != two
        finally:
            shutil.rmtree(one, ignore_errors=True)
            shutil.rmtree(two, ignore_errors=True)

    def test_stale_leftover_entry_is_removed_on_next_call(self):
        """A leftover file from a fully-finished previous run (older than
        the safety threshold) must be gone by the time the next call for
        the same target returns."""
        from code_indexer.server.services.agent_cli_isolation import (
            _STABLE_CWD_MIN_ENTRY_AGE_SECONDS,
            prepare_stable_neutral_cwd,
        )

        target = "/data/golden-repos/stale-leftover-repo"
        first = prepare_stable_neutral_cwd(target)
        try:
            leftover = os.path.join(first, "leftover.txt")
            with open(leftover, "w") as fh:
                fh.write("debris from a finished run")
            stale_time = time.time() - (_STABLE_CWD_MIN_ENTRY_AGE_SECONDS + 30)
            os.utime(leftover, (stale_time, stale_time))

            second = prepare_stable_neutral_cwd(target)

            assert second == first
            assert not os.path.exists(leftover)
        finally:
            shutil.rmtree(first, ignore_errors=True)

    def test_recent_entry_is_left_alone_defense_in_depth(self):
        """An entry younger than the safety threshold (as if a concurrent
        run against the SAME target is still using this directory) must
        NOT be swept out from under it."""
        from code_indexer.server.services.agent_cli_isolation import (
            prepare_stable_neutral_cwd,
        )

        target = "/data/golden-repos/recent-entry-repo"
        first = prepare_stable_neutral_cwd(target)
        try:
            fresh = os.path.join(first, "in-flight.txt")
            with open(fresh, "w") as fh:
                fh.write("still being used by a concurrent invocation")

            second = prepare_stable_neutral_cwd(target)

            assert second == first
            assert os.path.exists(fresh)
        finally:
            shutil.rmtree(first, ignore_errors=True)

    def test_lives_under_cidx_tmp_root_not_bare_tmp(self):
        from code_indexer.server.services.agent_cli_isolation import (
            CIDX_TMP_ROOT,
            prepare_stable_neutral_cwd,
        )

        path = prepare_stable_neutral_cwd("/data/golden-repos/tmp-root-repo")
        try:
            assert path.startswith(CIDX_TMP_ROOT)
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def test_cwd_is_never_inside_the_target(self):
        from code_indexer.server.services.agent_cli_isolation import (
            prepare_stable_neutral_cwd,
        )

        target = "/data/golden-repos/containment-repo"
        path = prepare_stable_neutral_cwd(target)
        try:
            assert not path.startswith(target)
            assert not target.startswith(path)
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def test_directory_itself_is_never_deleted_across_calls(self):
        from code_indexer.server.services.agent_cli_isolation import (
            prepare_stable_neutral_cwd,
        )

        target = "/data/golden-repos/persistent-dir-repo"
        first = prepare_stable_neutral_cwd(target)
        try:
            assert os.path.isdir(first)
            second = prepare_stable_neutral_cwd(target)
            assert os.path.isdir(second)
            assert os.path.isdir(first)
        finally:
            shutil.rmtree(first, ignore_errors=True)

    def test_empty_target_raises_value_error(self):
        from code_indexer.server.services.agent_cli_isolation import (
            prepare_stable_neutral_cwd,
        )

        with pytest.raises(ValueError):
            prepare_stable_neutral_cwd("")

    def test_recently_touched_nested_file_survives_even_with_stale_top_level_mtime(
        self,
    ):
        """A directory's own mtime only changes when a DIRECT child is
        added or removed, not when a file several levels below it is
        written. A long-running call's top-level work/ directory can sit
        at its creation-time mtime for its whole lifetime while a nested
        file underneath it is still being written -- age must be judged by
        the newest mtime ANYWHERE in the subtree, not the top-level entry
        alone, or a live call's directory gets deleted out from under it."""
        from code_indexer.server.services.agent_cli_isolation import (
            _STABLE_CWD_MIN_ENTRY_AGE_SECONDS,
            prepare_stable_neutral_cwd,
        )

        target = "/data/golden-repos/nested-race-repo"
        first = prepare_stable_neutral_cwd(target)
        try:
            work_dir = os.path.join(first, "work")
            nested_dir = os.path.join(work_dir, "nested", "deep")
            os.makedirs(nested_dir)
            nested_file = os.path.join(nested_dir, "still-running.txt")
            with open(nested_file, "w") as fh:
                fh.write("actively being written by a long-running call")

            stale_time = time.time() - (2 * _STABLE_CWD_MIN_ENTRY_AGE_SECONDS)
            os.utime(work_dir, (stale_time, stale_time))

            second = prepare_stable_neutral_cwd(target)

            assert second == first
            assert os.path.exists(nested_file), (
                "a recently-written nested file must protect its whole "
                "ancestor directory from removal, even when the top-level "
                "directory's own mtime looks stale"
            )
        finally:
            shutil.rmtree(first, ignore_errors=True)

    def test_fully_stale_nested_tree_is_removed(self):
        """When every mtime in a top-level entry's subtree is older than
        the safety threshold, the whole entry is removed -- the nested-mtime
        fix must not disable cleanup altogether, only make it safe against
        an entry that is still actively being written to."""
        from code_indexer.server.services.agent_cli_isolation import (
            _STABLE_CWD_MIN_ENTRY_AGE_SECONDS,
            prepare_stable_neutral_cwd,
        )

        target = "/data/golden-repos/fully-stale-nested-repo"
        first = prepare_stable_neutral_cwd(target)
        try:
            work_dir = os.path.join(first, "work")
            nested_dir = os.path.join(work_dir, "nested")
            os.makedirs(nested_dir)
            nested_file = os.path.join(nested_dir, "old.txt")
            with open(nested_file, "w") as fh:
                fh.write("debris from a long-finished run")

            stale_time = time.time() - (2 * _STABLE_CWD_MIN_ENTRY_AGE_SECONDS)
            os.utime(nested_file, (stale_time, stale_time))
            os.utime(nested_dir, (stale_time, stale_time))
            os.utime(work_dir, (stale_time, stale_time))

            second = prepare_stable_neutral_cwd(target)

            assert second == first
            assert not os.path.exists(work_dir)
        finally:
            shutil.rmtree(first, ignore_errors=True)


# ---------------------------------------------------------------------------
# MCP config document / file
# ---------------------------------------------------------------------------


class TestBuildMcpConfigDocument:
    def test_document_names_only_cidx_local(self):
        from code_indexer.server.services.agent_cli_isolation import (
            build_mcp_config_document,
        )

        doc = build_mcp_config_document(port=8000, auth_header_value="Basic abc")
        assert list(doc["mcpServers"].keys()) == ["cidx-local"]

    def test_document_uses_localhost_url_with_port(self):
        from code_indexer.server.services.agent_cli_isolation import (
            build_mcp_config_document,
        )

        doc = build_mcp_config_document(port=9123, auth_header_value="Basic abc")
        assert doc["mcpServers"]["cidx-local"]["url"] == "http://localhost:9123/mcp"

    def test_document_carries_authorization_header(self):
        from code_indexer.server.services.agent_cli_isolation import (
            build_mcp_config_document,
        )

        doc = build_mcp_config_document(port=8000, auth_header_value="Basic xyz123")
        assert (
            doc["mcpServers"]["cidx-local"]["headers"]["Authorization"]
            == "Basic xyz123"
        )

    def test_invalid_port_raises_value_error(self):
        from code_indexer.server.services.agent_cli_isolation import (
            build_mcp_config_document,
        )

        with pytest.raises(ValueError):
            build_mcp_config_document(port=0, auth_header_value="Basic abc")

    def test_empty_auth_header_raises_value_error(self):
        from code_indexer.server.services.agent_cli_isolation import (
            build_mcp_config_document,
        )

        with pytest.raises(ValueError):
            build_mcp_config_document(port=8000, auth_header_value="")


class TestWriteMcpConfigFile:
    def test_writes_json_file_with_expected_document(self, tmp_path):
        from code_indexer.server.services.agent_cli_isolation import (
            write_mcp_config_file,
        )

        path = write_mcp_config_file(
            port=8000, auth_header_value="Basic abc", dest_dir=str(tmp_path)
        )
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        assert doc["mcpServers"]["cidx-local"]["url"] == "http://localhost:8000/mcp"

    def test_file_permissions_are_owner_only(self, tmp_path):
        from code_indexer.server.services.agent_cli_isolation import (
            write_mcp_config_file,
        )

        path = write_mcp_config_file(
            port=8000, auth_header_value="Basic abc", dest_dir=str(tmp_path)
        )
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == stat.S_IRUSR | stat.S_IWUSR

    def test_file_lives_inside_dest_dir(self, tmp_path):
        from code_indexer.server.services.agent_cli_isolation import (
            write_mcp_config_file,
        )

        path = write_mcp_config_file(
            port=8000, auth_header_value="Basic abc", dest_dir=str(tmp_path)
        )
        assert os.path.dirname(path) == str(tmp_path)


class TestCreatePrivateMcpConfigDir:
    def test_creates_a_directory(self):
        from code_indexer.server.services.agent_cli_isolation import (
            create_private_mcp_config_dir,
        )

        path = create_private_mcp_config_dir()
        try:
            assert os.path.isdir(path)
        finally:
            os.rmdir(path)

    def test_distinct_from_neutral_scratch_dir(self):
        """The MCP config dir and the agent's cwd are never the same
        directory — a fresh call to each returns different paths."""
        from code_indexer.server.services.agent_cli_isolation import (
            create_private_mcp_config_dir,
            prepare_stable_neutral_cwd,
        )

        scratch = prepare_stable_neutral_cwd("/data/golden-repos/mcp-dir-distinct-repo")
        mcp_dir = create_private_mcp_config_dir()
        try:
            assert scratch != mcp_dir
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            os.rmdir(mcp_dir)


class TestRemoveMcpConfigFile:
    def test_removes_file_and_its_private_dir(self, tmp_path):
        from code_indexer.server.services.agent_cli_isolation import (
            remove_mcp_config_file,
            write_mcp_config_file,
        )

        sub = tmp_path / "private"
        sub.mkdir()
        path = write_mcp_config_file(
            port=8000, auth_header_value="Basic abc", dest_dir=str(sub)
        )
        remove_mcp_config_file(path)
        assert not os.path.exists(path)
        assert not os.path.exists(str(sub))

    def test_none_path_is_a_no_op(self):
        from code_indexer.server.services.agent_cli_isolation import (
            remove_mcp_config_file,
        )

        remove_mcp_config_file(None)  # must not raise

    def test_missing_file_does_not_raise(self):
        from code_indexer.server.services.agent_cli_isolation import (
            remove_mcp_config_file,
        )

        remove_mcp_config_file("/nonexistent/path/for/this/test/config.json")


class TestTryBuildMcpConfigFile:
    def test_returns_none_when_registration_service_unavailable(self):
        from code_indexer.server.services.agent_cli_isolation import (
            try_build_mcp_config_file,
        )

        with patch(
            "code_indexer.server.services.mcp_self_registration_service."
            "MCPSelfRegistrationService.get_instance",
            return_value=None,
        ):
            assert try_build_mcp_config_file() is None

    def test_returns_path_in_its_own_private_dir_when_header_and_port_available(
        self,
    ):
        from code_indexer.server.services.agent_cli_isolation import (
            try_build_mcp_config_file,
            remove_mcp_config_file,
        )

        fake_svc = MagicMock()
        fake_svc.get_cached_auth_header_value.return_value = None
        fake_svc.build_auth_header_from_creds.return_value = "Basic abc"
        fake_config_service = MagicMock()
        fake_config_service.get_config.return_value.port = 8000

        with patch(
            "code_indexer.server.services.mcp_self_registration_service."
            "MCPSelfRegistrationService.get_instance",
            return_value=fake_svc,
        ):
            with patch(
                "code_indexer.server.services.agent_cli_isolation.get_config_service",
                return_value=fake_config_service,
            ):
                path = try_build_mcp_config_file()
        try:
            assert path is not None
            assert os.path.isfile(path)
        finally:
            remove_mcp_config_file(path)

    def test_prefers_cached_header_over_registering_again(self):
        """The cached header path is tried first, so a normal call never
        shells out to `claude mcp add` on its own account."""
        from code_indexer.server.services.agent_cli_isolation import (
            try_build_mcp_config_file,
            remove_mcp_config_file,
        )

        fake_svc = MagicMock()
        fake_svc.get_cached_auth_header_value.return_value = "Basic cached"
        fake_config_service = MagicMock()
        fake_config_service.get_config.return_value.port = 8000

        with patch(
            "code_indexer.server.services.mcp_self_registration_service."
            "MCPSelfRegistrationService.get_instance",
            return_value=fake_svc,
        ):
            with patch(
                "code_indexer.server.services.agent_cli_isolation.get_config_service",
                return_value=fake_config_service,
            ):
                path = try_build_mcp_config_file()
        try:
            assert path is not None
            fake_svc.build_auth_header_from_creds.assert_not_called()
        finally:
            remove_mcp_config_file(path)

    def test_already_registered_case_still_produces_header_and_mcp_config(self):
        """Regression: when cidx-local is already registered from a prior
        process, ensure_registered()'s already-registered branch returns
        True without ever calling register_in_claude_code(), so
        build_auth_header_from_creds() alone returns None even though
        registration genuinely succeeded. This drives the real
        MCPSelfRegistrationService — only the two CLI-probe methods
        (claude_cli_available, is_already_registered) are stubbed, plus
        subprocess.run so register_in_claude_code()'s `claude mcp add` side
        effect never touches the real system — proving the third fallback
        (build_header_from_stored_credentials) still produces a usable
        header, and that --mcp-config ends up in the built argv."""
        from code_indexer.server.services.agent_cli_isolation import (
            build_claude_isolation_args,
            remove_mcp_config_file,
            try_build_mcp_config_file,
        )
        from code_indexer.server.services.mcp_self_registration_service import (
            MCPSelfRegistrationService,
        )
        from code_indexer.server.utils.config_manager import (
            MCPSelfRegistrationConfig,
        )

        mock_config_manager = MagicMock()
        mock_config = MagicMock()
        mock_config.port = 8123
        mock_config.mcp_self_registration = MCPSelfRegistrationConfig()
        mock_config_manager.load_config.return_value = mock_config

        mock_credential_manager = MagicMock()
        mock_credential_manager.generate_credential_audited.return_value = {
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
        }

        real_service = MCPSelfRegistrationService(
            config_manager=mock_config_manager,
            mcp_credential_manager=mock_credential_manager,
        )

        fake_config_service = MagicMock()
        fake_config_service.get_config.return_value.port = 8123

        with patch.object(real_service, "claude_cli_available", return_value=True):
            with patch.object(real_service, "is_already_registered", return_value=True):
                with patch("subprocess.run", return_value=MagicMock(returncode=0)):
                    with patch(
                        "code_indexer.server.services.mcp_self_registration_service."
                        "MCPSelfRegistrationService.get_instance",
                        return_value=real_service,
                    ):
                        with patch(
                            "code_indexer.server.services.agent_cli_isolation."
                            "get_config_service",
                            return_value=fake_config_service,
                        ):
                            path = try_build_mcp_config_file()
        try:
            assert path is not None, (
                "already-registered case must still produce a usable header"
            )
            args = build_claude_isolation_args(
                analysis_dir="/golden-repos", mcp_config_path=path
            )
            assert "--mcp-config" in args
        finally:
            remove_mcp_config_file(path)


# ---------------------------------------------------------------------------
# build_claude_isolation_args — the argv fragment claude_invoker.py consumes
# ---------------------------------------------------------------------------


class TestBuildClaudeIsolationArgs:
    def test_includes_neutral_add_dir(self):
        from code_indexer.server.services.agent_cli_isolation import (
            build_claude_isolation_args,
        )

        args = build_claude_isolation_args(
            analysis_dir="/golden-repos/example-repo",
            mcp_config_path=None,
        )
        assert "--setting-sources" in args
        assert args[args.index("--setting-sources") + 1] == "user"
        assert "--add-dir" in args
        assert args[args.index("--add-dir") + 1] == "/golden-repos/example-repo"

    def test_no_tool_restriction_flags_present(self):
        """The agent keeps its full built-in tool set (Bash, Write, Edit,
        Glob, Grep, Read) — no --restricted, no --tools flag at all."""
        from code_indexer.server.services.agent_cli_isolation import (
            build_claude_isolation_args,
        )

        args = build_claude_isolation_args(
            analysis_dir="/golden-repos", mcp_config_path=None
        )
        assert "--restricted" not in args
        assert "--tools" not in args

    def test_always_includes_dangerously_skip_permissions(self):
        """--dangerously-skip-permissions is what lets Bash/Write run
        non-interactively; every non-exempt flow keeps it."""
        from code_indexer.server.services.agent_cli_isolation import (
            build_claude_isolation_args,
        )

        args = build_claude_isolation_args(
            analysis_dir="/golden-repos", mcp_config_path=None
        )
        assert "--dangerously-skip-permissions" in args

    def test_strict_mcp_config_present_even_without_a_config_path(self):
        """--strict-mcp-config is unconditional: even when no cidx-local
        config file could be built, the flag alone still excludes every
        globally-registered server from the session (zero servers, rather
        than falling back to whatever the account has configured)."""
        from code_indexer.server.services.agent_cli_isolation import (
            build_claude_isolation_args,
        )

        args = build_claude_isolation_args(
            analysis_dir="/golden-repos", mcp_config_path=None
        )
        assert "--strict-mcp-config" in args
        assert "--mcp-config" not in args

    def test_mcp_config_path_adds_mcp_config_flag(self):
        from code_indexer.server.services.agent_cli_isolation import (
            build_claude_isolation_args,
        )

        args = build_claude_isolation_args(
            analysis_dir="/golden-repos",
            mcp_config_path="/home/user_a/.tmp/cidx-mcp-config-x/mcp-config.json",
        )
        assert "--strict-mcp-config" in args
        assert "--mcp-config" in args
        assert (
            args[args.index("--mcp-config") + 1]
            == "/home/user_a/.tmp/cidx-mcp-config-x/mcp-config.json"
        )

    def test_empty_analysis_dir_raises_value_error(self):
        from code_indexer.server.services.agent_cli_isolation import (
            build_claude_isolation_args,
        )

        with pytest.raises(ValueError):
            build_claude_isolation_args(analysis_dir="", mcp_config_path=None)


# ---------------------------------------------------------------------------
# build_self_monitoring_claude_args / build_self_monitoring_claude_allowed_tools
# The self-monitoring scan queries the server's own
# log DB through a server-owned read-only entry point
# (code_indexer.server.self_monitoring.log_query) instead of the raw sqlite3
# CLI -- the installed sqlite3 build has no -safe flag, so .shell/.system/
# ATTACH/load_extension could not be closed by CLI flags alone. The Bash
# allow rule is pinned to this server process's own interpreter running that
# module; the log database path is never a CLI argument (env var only, set
# by the invoker), so there is nothing path-shaped for the rule to pin.
# ---------------------------------------------------------------------------


class TestBuildSelfMonitoringLogQueryCommandPrefix:
    def test_pinned_to_this_process_own_interpreter(self):
        import sys

        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_log_query_command_prefix,
        )

        prefix = build_self_monitoring_log_query_command_prefix()
        assert prefix.startswith(sys.executable)

    def test_pinned_to_the_log_query_module(self):
        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_log_query_command_prefix,
        )

        prefix = build_self_monitoring_log_query_command_prefix()
        assert prefix.endswith("-m code_indexer.server.self_monitoring.log_query")


class TestBuildSelfMonitoringClaudeAllowedTools:
    def test_pins_bash_to_the_log_query_entry_point(self):
        import sys

        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_claude_allowed_tools,
        )

        tools = build_self_monitoring_claude_allowed_tools()
        joined = " ".join(tools)
        assert (
            f"Bash({sys.executable} -m code_indexer.server.self_monitoring.log_query *)"
        ) in joined

    def test_takes_no_log_db_path_argument(self):
        """The log database path is never a CLI argument -- there is
        nothing path-shaped left in the allow rule for the caller to
        supply, and the function signature reflects that."""
        import inspect

        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_claude_allowed_tools,
        )

        assert (
            inspect.signature(build_self_monitoring_claude_allowed_tools).parameters
            == {}
        )

    def test_does_not_allowlist_bare_read_glob_grep(self):
        """A bare Read/Glob/Grep allow entry makes every call execute
        without a prompt, anywhere on the filesystem -- which would UNDO
        the repo_root confinement this flow already gets for free from the
        default (no-rule) behaviour. They are deliberately absent here."""
        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_claude_allowed_tools,
        )

        tools = build_self_monitoring_claude_allowed_tools()
        assert "Read" not in tools
        assert "Glob" not in tools
        assert "Grep" not in tools


class TestBuildSelfMonitoringClaudeArgs:
    def test_never_includes_dangerously_skip_permissions(self):
        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_claude_args,
        )

        args = build_self_monitoring_claude_args()
        assert "--dangerously-skip-permissions" not in args

    def test_includes_allowed_tools_with_the_log_query_pin(self):
        import sys

        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_claude_args,
        )

        args = build_self_monitoring_claude_args()
        assert "--allowedTools" in args
        allowed_value = args[args.index("--allowedTools") + 1]
        assert "code_indexer.server.self_monitoring.log_query" in allowed_value
        assert sys.executable in allowed_value

    def test_never_includes_disallowed_tools(self):
        """A --disallowedTools deny list for cat/grep/find/ls would also
        block them INSIDE the working directory, where they are how the
        agent searches the codebase (Claude Code 2.1.283 has no Glob/Grep
        tools). --disallowedTools must never appear in these args."""
        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_claude_args,
        )

        args = build_self_monitoring_claude_args()
        assert "--disallowedTools" not in args

    def test_includes_settings_blocking_reads_outside_working_directories(self):
        """permissions.blockReadsOutsideWorkingDirectories (verified against
        the official settings reference) fences file tools AND the
        built-in read-only Bash commands outside the working directory, in
        every permission mode -- closing the outside-cwd read gap without
        touching in-repo code search."""
        import json

        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_claude_args,
        )

        args = build_self_monitoring_claude_args()
        assert "--settings" in args
        settings_value = args[args.index("--settings") + 1]
        parsed = json.loads(settings_value)
        assert parsed["permissions"]["blockReadsOutsideWorkingDirectories"] is True

    def test_includes_permission_mode_dont_ask(self):
        """dontAsk auto-denies every call that would otherwise prompt,
        rather than hanging or interactively prompting in headless -p mode,
        while still running pre-approved (allowedTools) calls."""
        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_claude_args,
        )

        args = build_self_monitoring_claude_args()
        assert "--permission-mode" in args
        assert args[args.index("--permission-mode") + 1] == "dontAsk"

    def test_includes_strict_mcp_config_with_zero_mcp_servers(self):
        """This flow calls no MCP tool today, so no --mcp-config is ever
        added; --strict-mcp-config alone means zero MCP servers."""
        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_claude_args,
        )

        args = build_self_monitoring_claude_args()
        assert "--strict-mcp-config" in args
        assert "--mcp-config" not in args

    def test_takes_no_log_db_path_argument(self):
        import inspect

        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_claude_args,
        )

        assert inspect.signature(build_self_monitoring_claude_args).parameters == {}

    def test_includes_setting_sources_user(self):
        """Without --setting-sources, project/local .claude/settings*.json
        allow lists and additionalDirectories entries merge in and widen
        this flow's permissions beyond --allowedTools/--settings. 'user'
        (never '' -- an empty value would also drop the service user's own
        hooks) restricts loading to ONLY the service account's user-level
        settings.json, matching what every other flow already gets via
        build_claude_isolation_args."""
        from code_indexer.server.services.agent_cli_isolation import (
            build_self_monitoring_claude_args,
        )

        args = build_self_monitoring_claude_args()
        assert "--setting-sources" in args
        assert args[args.index("--setting-sources") + 1] == "user"
