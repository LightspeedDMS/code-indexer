"""
Unit tests for AC7: Delta merge has source code access.

Story #216 AC7:
- build_delta_merge_prompt includes clone_path for changed/new repos
- build_delta_merge_prompt includes search_code MCP tool guidance
- invoke_delta_merge_file passes allowed_tools and dangerously_skip_permissions
"""

from unittest.mock import patch

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
    """AC7: invoke_delta_merge_file passes allowed_tools and dangerously_skip_permissions."""

    def test_invoke_delta_merge_file_passes_search_code_tool(self, tmp_path):
        """AC7: invoke_delta_merge_file passes --allowedTools mcp__cidx-local__search_code."""
        analyzer = _make_analyzer(tmp_path)
        updated_content = "# Domain Analysis: auth\n\nUpdated content."

        with patch(
            _SUBPROCESS_PATH,
            side_effect=_make_edit_side_effect(
                "_delta_merge_*.md", updated_content, tmp_path
            ),
        ) as mock_run:
            analyzer.invoke_delta_merge_file(
                domain_name="auth",
                existing_content=EXISTING_CONTENT,
                merge_prompt="test prompt",
                timeout=60,
                max_turns=5,
                temp_dir=tmp_path,
            )

        cmd = mock_run.call_args[0][0]
        assert "--allowedTools" in cmd, (
            "invoke_delta_merge_file must pass --allowedTools"
        )
        allowed_tools_idx = cmd.index("--allowedTools")
        assert cmd[allowed_tools_idx + 1] == "mcp__cidx-local__search_code", (
            f"--allowedTools must be mcp__cidx-local__search_code, got: {cmd[allowed_tools_idx + 1]}"
        )
        assert "--dangerously-skip-permissions" in cmd, (
            "invoke_delta_merge_file must pass --dangerously-skip-permissions"
        )
