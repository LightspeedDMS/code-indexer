"""
Unit tests for AC7: Delta merge has source code access.

Story #216 AC7:
- build_delta_merge_prompt includes clone_path for changed/new repos
- build_delta_merge_prompt includes search_code MCP tool guidance
- invoke_delta_merge passes allowed_tools containing search_code
"""

from pathlib import Path
from unittest.mock import Mock, patch


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

        assert "/repos/auth-svc" in prompt, "clone_path must appear in prompt for changed repos"

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

        assert "/repos/new-svc" in prompt, "clone_path must appear in prompt for new repos"

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


class TestInvokeDeltaMergeAllowedTools:
    """AC7: invoke_delta_merge passes allowed_tools to _invoke_claude_cli."""

    def test_invoke_delta_merge_passes_search_code_tool(self, tmp_path):
        """AC7: invoke_delta_merge passes allowed_tools containing search_code."""
        analyzer = _make_analyzer(tmp_path)

        with patch.object(
            analyzer, "_invoke_claude_cli", return_value="# Domain Analysis: auth\n\nContent."
        ) as mock_invoke:
            analyzer.invoke_delta_merge(prompt="test", timeout=60, max_turns=5)

        args = mock_invoke.call_args[0] if mock_invoke.call_args[0] else []
        kwargs = mock_invoke.call_args[1] if mock_invoke.call_args[1] else {}

        allowed_tools_value = None
        if len(args) >= 4:
            allowed_tools_value = args[3]
        elif "allowed_tools" in kwargs:
            allowed_tools_value = kwargs["allowed_tools"]

        assert allowed_tools_value is not None, "allowed_tools must be passed to _invoke_claude_cli"
        assert "search_code" in str(allowed_tools_value), (
            f"allowed_tools must contain search_code, got: {allowed_tools_value}"
        )
