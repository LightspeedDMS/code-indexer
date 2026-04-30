"""
Unit tests for AC7: Delta merge has source code access.

Story #216 AC7:
- build_delta_merge_prompt includes clone_path for changed/new repos
- build_delta_merge_prompt includes search_code MCP tool guidance
- invoke_delta_merge_file passes allowed_tools and dangerously_skip_permissions
"""

_SUBPROCESS_PATH = "code_indexer.global_repos.dependency_map_analyzer.subprocess.run"
_MTIME_TICK_S = 0.05


def _make_subprocess_result(returncode=0, stdout="FILE_EDIT_COMPLETE", stderr=""):
    from unittest.mock import MagicMock

    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _make_edit_side_effect(temp_glob: str, new_content: str, tmp_path):
    """Return a subprocess.run side_effect that writes new_content to the temp file."""
    import time
    from pathlib import Path

    def _side_effect(*args, **kwargs):
        time.sleep(_MTIME_TICK_S)
        matched = list(Path(str(tmp_path)).glob(temp_glob))
        if matched:
            matched[0].write_text(new_content)
        return _make_subprocess_result()

    return _side_effect


def _make_analyzer(tmp_path):
    from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer

    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=600,
    )


EXISTING_CONTENT = "# Domain Analysis: auth\n\n## Overview\nTest content.\n"


class TestBuildDeltaMergePromptClonePaths:
    """AC7: build_delta_merge_prompt includes clone_path and search_code guidance."""

    def test_includes_clone_path_for_changed_repos(self, tmp_path):
        """AC7: Prompt includes clone_path for each changed repo dict."""
        analyzer = _make_analyzer(tmp_path)
        changed_repos = [{"alias": "auth-svc", "clone_path": "/repos/auth-svc"}]

        prompt = analyzer.build_delta_merge_prompt(
            domain_name="auth",
            existing_content=EXISTING_CONTENT,
            changed_repos=changed_repos,
            new_repos=[],
            removed_repos=[],
            domain_list=["auth", "billing"],
        )

        assert "/repos/auth-svc" in prompt, (
            "clone_path must appear in prompt for changed repos"
        )

    def test_includes_clone_path_for_new_repos(self, tmp_path):
        """AC7: Prompt includes clone_path for new repos."""
        analyzer = _make_analyzer(tmp_path)
        new_repos = [{"alias": "new-svc", "clone_path": "/repos/new-svc"}]

        prompt = analyzer.build_delta_merge_prompt(
            domain_name="auth",
            existing_content=EXISTING_CONTENT,
            changed_repos=[],
            new_repos=new_repos,
            removed_repos=[],
            domain_list=["auth"],
        )

        assert "/repos/new-svc" in prompt, (
            "clone_path must appear in prompt for new repos"
        )

    def test_includes_search_code_guidance(self, tmp_path):
        """AC7: Prompt includes guidance about search_code MCP tool."""
        analyzer = _make_analyzer(tmp_path)
        changed_repos = [{"alias": "auth-svc", "clone_path": "/repos/auth-svc"}]

        prompt = analyzer.build_delta_merge_prompt(
            domain_name="auth",
            existing_content=EXISTING_CONTENT,
            changed_repos=changed_repos,
            new_repos=[],
            removed_repos=[],
            domain_list=["auth"],
        )

        assert "search_code" in prompt, "Prompt must reference search_code MCP tool"

    def test_string_aliases_still_work(self, tmp_path):
        """AC7: String-only aliases (old convention) still appear in prompt."""
        analyzer = _make_analyzer(tmp_path)

        prompt = analyzer.build_delta_merge_prompt(
            domain_name="auth",
            existing_content=EXISTING_CONTENT,
            changed_repos=["auth-svc"],  # plain strings
            new_repos=[],
            removed_repos=[],
            domain_list=["auth"],
        )

        assert "auth-svc" in prompt


class TestInvokeDeltaMergeFileAllowedTools:
    """AC7 (Bug #936): invoke_delta_merge_file routes through the dispatcher with the correct flow.

    Bug #936 changed invoke_delta_merge_file to call _dispatch_via_flow with
    flow='dependency_map_delta_merge' instead of calling subprocess.run directly.
    The --allowedTools CLI flag is not forwarded because IntelligenceCliInvoker
    has no tool-restriction param and Codex has no equivalent.
    """

    def test_invoke_delta_merge_file_passes_search_code_tool(self, tmp_path):
        """AC7 (Bug #936): invoke_delta_merge_file dispatches with flow='dependency_map_delta_merge'."""
        from unittest.mock import MagicMock

        from code_indexer.global_repos.dependency_map_analyzer import (
            DependencyMapAnalyzer,
        )

        # Inject a mock dispatcher via the documented DI constructor parameter.
        mock_dispatcher = MagicMock()
        mock_dispatch_result = MagicMock()
        mock_dispatch_result.success = True
        mock_dispatch_result.output = "FILE_EDIT_COMPLETE"
        mock_dispatch_result.was_failover = False
        mock_dispatcher.dispatch.return_value = mock_dispatch_result

        analyzer = DependencyMapAnalyzer(
            golden_repos_root=tmp_path,
            cidx_meta_path=tmp_path / "cidx-meta",
            pass_timeout=600,
            cli_dispatcher=mock_dispatcher,
        )

        analyzer.invoke_delta_merge_file(
            domain_name="auth",
            existing_content=EXISTING_CONTENT,
            merge_prompt="test prompt",
            timeout=60,
            max_turns=5,
            temp_dir=tmp_path,
        )

        mock_dispatcher.dispatch.assert_called_once()
        call_kwargs = mock_dispatcher.dispatch.call_args
        flow_used = call_kwargs.kwargs.get("flow") or call_kwargs[1].get("flow")
        assert flow_used == "dependency_map_delta_merge", (
            f"invoke_delta_merge_file must dispatch with flow='dependency_map_delta_merge', got: {flow_used}"
        )
