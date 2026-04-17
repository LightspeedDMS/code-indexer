"""
Tests for the lifecycle detection invoker and frontmatter split helper.

Covers:
- invoke_lifecycle_detection returns None on CLI timeout, malformed YAML,
  non-zero exit, missing lifecycle key, and empty output
- invoke_lifecycle_detection returns the full parsed dict on success
- invoke_lifecycle_detection passes Phase 2 timeouts and repo_path to wrapper
- split_frontmatter_and_body splits standard YAML frontmatter, handles
  missing frontmatter, missing closing delimiter, and nested lifecycle block
"""

import sys
from pathlib import Path
from unittest.mock import patch

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Phase 2 timeout constants (from spec)
PHASE2_SHELL_TIMEOUT = 180
PHASE2_OUTER_TIMEOUT = 240

# Minimal valid lifecycle YAML output from Claude
_VALID_LIFECYCLE_YAML = """\
lifecycle_schema_version: 1
lifecycle:
  branches_to_env:
    main: production
  detected_sources:
    - github_actions:deploy.yml
  confidence: high
  claude_notes: |
    Main deploys to production via CI.
"""

# Expected parsed dict for the YAML above — used in full equality assertion
_VALID_LIFECYCLE_DICT = {
    "lifecycle_schema_version": 1,
    "lifecycle": {
        "branches_to_env": {"main": "production"},
        "detected_sources": ["github_actions:deploy.yml"],
        "confidence": "high",
        "claude_notes": "Main deploys to production via CI.\n",
    },
}


def _patch_invoke(success: bool, output: str):
    """Return a context manager that patches invoke_claude_cli."""
    return patch(
        "code_indexer.global_repos.repo_analyzer.invoke_claude_cli",
        return_value=(success, output),
    )


class TestInvokeLifecycleDetection:
    """Tests for invoke_lifecycle_detection()."""

    def test_returns_none_on_cli_failure(self, tmp_path):
        """Returns None when invoke_claude_cli reports failure (non-zero exit)."""
        from code_indexer.global_repos.repo_analyzer import invoke_lifecycle_detection

        with _patch_invoke(False, "error: Claude CLI returned non-zero: 1"):
            result = invoke_lifecycle_detection(str(tmp_path))

        assert result is None

    def test_returns_none_on_cli_timeout(self, tmp_path):
        """Returns None when invoke_claude_cli reports a timeout failure."""
        from code_indexer.global_repos.repo_analyzer import invoke_lifecycle_detection

        with _patch_invoke(False, "Claude CLI timed out after 240s"):
            result = invoke_lifecycle_detection(str(tmp_path))

        assert result is None

    def test_returns_none_on_malformed_yaml(self, tmp_path):
        """Returns None when Claude output is not valid YAML."""
        from code_indexer.global_repos.repo_analyzer import invoke_lifecycle_detection

        with _patch_invoke(True, "this is: not: valid: yaml: [[["):
            result = invoke_lifecycle_detection(str(tmp_path))

        assert result is None

    def test_returns_none_when_lifecycle_key_absent(self, tmp_path):
        """Returns None when output is valid YAML but has no 'lifecycle' key."""
        from code_indexer.global_repos.repo_analyzer import invoke_lifecycle_detection

        with _patch_invoke(True, "some_key: value\nother_key: 42\n"):
            result = invoke_lifecycle_detection(str(tmp_path))

        assert result is None

    def test_returns_none_on_empty_output(self, tmp_path):
        """Returns None when Claude output is empty string."""
        from code_indexer.global_repos.repo_analyzer import invoke_lifecycle_detection

        with _patch_invoke(True, ""):
            result = invoke_lifecycle_detection(str(tmp_path))

        assert result is None

    def test_returns_full_parsed_dict_on_valid_lifecycle_yaml(self, tmp_path):
        """Returns the full parsed dict (matching _VALID_LIFECYCLE_DICT) on success."""
        from code_indexer.global_repos.repo_analyzer import invoke_lifecycle_detection

        with _patch_invoke(True, _VALID_LIFECYCLE_YAML):
            result = invoke_lifecycle_detection(str(tmp_path))

        assert result == _VALID_LIFECYCLE_DICT

    def test_uses_phase2_timeouts(self, tmp_path):
        """invoke_lifecycle_detection calls invoke_claude_cli with Phase 2 timeouts."""
        from code_indexer.global_repos.repo_analyzer import invoke_lifecycle_detection

        with patch(
            "code_indexer.global_repos.repo_analyzer.invoke_claude_cli",
            return_value=(True, _VALID_LIFECYCLE_YAML),
        ) as mock_invoke:
            invoke_lifecycle_detection(str(tmp_path))

        mock_invoke.assert_called_once()
        _, _, shell_t, outer_t = mock_invoke.call_args[0]
        assert shell_t == PHASE2_SHELL_TIMEOUT
        assert outer_t == PHASE2_OUTER_TIMEOUT

    def test_passes_repo_path_as_first_arg(self, tmp_path):
        """invoke_lifecycle_detection passes repo_path as first argument to wrapper."""
        from code_indexer.global_repos.repo_analyzer import invoke_lifecycle_detection

        with patch(
            "code_indexer.global_repos.repo_analyzer.invoke_claude_cli",
            return_value=(True, _VALID_LIFECYCLE_YAML),
        ) as mock_invoke:
            invoke_lifecycle_detection(str(tmp_path))

        assert mock_invoke.call_args[0][0] == str(tmp_path)


class TestSplitFrontmatterAndBody:
    """Tests for split_frontmatter_and_body helper."""

    def test_splits_standard_yaml_frontmatter(self):
        """Correctly splits content with opening and closing --- delimiters."""
        from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body

        content = "---\nname: test\nurl: http://x\n---\n# Title\n\nBody text.\n"
        fm_dict, body = split_frontmatter_and_body(content)

        assert fm_dict == {"name": "test", "url": "http://x"}
        assert "# Title" in body

    def test_returns_empty_dict_when_no_frontmatter(self):
        """Returns ({}, content) when content does not start with ---."""
        from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body

        content = "# Plain markdown\n\nNo frontmatter here.\n"
        fm_dict, body = split_frontmatter_and_body(content)

        assert fm_dict == {}
        assert body == content

    def test_returns_empty_dict_when_no_closing_delimiter(self):
        """Returns ({}, content) when opening --- has no matching closing ---."""
        from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body

        content = "---\nname: broken\n"
        fm_dict, body = split_frontmatter_and_body(content)

        assert fm_dict == {}
        assert body == content

    def test_nested_lifecycle_block_preserved(self):
        """Nested lifecycle: dict round-trips correctly via yaml.safe_load."""
        from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body

        content = (
            "---\n"
            "name: repo\n"
            "lifecycle:\n"
            "  confidence: high\n"
            "  branches_to_env:\n"
            "    main: production\n"
            "---\n"
            "# Body\n"
        )
        fm_dict, body = split_frontmatter_and_body(content)

        assert fm_dict["lifecycle"]["confidence"] == "high"
        assert fm_dict["lifecycle"]["branches_to_env"]["main"] == "production"
        assert "# Body" in body
