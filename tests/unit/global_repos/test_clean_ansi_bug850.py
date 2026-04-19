"""
Bug #850: ANSI private-mode sequences ESC[>4m bypass _clean_claude_output,
causing YAML parse failure and infinite lifecycle_backfill loop.

Tests:
1. test_clean_claude_output_strips_private_ansi_gt
   — ESC[>4m (the exact production failure sequence) must not survive cleaning
2. test_clean_claude_output_strips_all_private_param_bytes
   — All private parameter bytes (>, <, =, :) must be stripped (generalized)
3. test_yaml_parse_succeeds_after_cleaning_private_ansi
   — YAML parse must succeed on output containing ESC[>4m wrappers
4. test_clean_claude_output_existing_sequences_still_work
   — Existing sequences (ESC[0m, ESC[1;32m, ESC[?25h) must still be stripped
5. test_invoke_claude_cli_sets_no_color_env
   — invoke_claude_cli must set NO_COLOR=1 in the subprocess environment
"""

from unittest.mock import MagicMock, patch

import yaml

from code_indexer.global_repos.repo_analyzer import (
    _clean_claude_output,
    invoke_claude_cli,
)

# Named constants to avoid magic numbers in subprocess timeout tests
_SHELL_TIMEOUT_SECONDS = 10
_OUTER_TIMEOUT_SECONDS = 30


class TestCleanClaudeOutputPrivateAnsi:
    """Tests for Bug #850: CSI private parameter byte sequences not stripped."""

    def test_clean_claude_output_strips_private_ansi_gt(self):
        """ESC[>4m (exact production failure: private mode set, gt param byte) must be fully removed."""
        output = "\x1b[>4msome yaml content\x1b[>4m"
        result = _clean_claude_output(output)
        assert "[>4m" not in result
        assert "\x1b" not in result
        assert "some yaml content" in result

    def test_clean_claude_output_strips_all_private_param_bytes(self):
        """All ANSI private parameter bytes (>, <, =, :) must be fully stripped (generalized coverage)."""
        for char in [">", "<", "=", ":"]:
            output = f"\x1b[{char}4mtext"
            result = _clean_claude_output(output)
            assert f"[{char}4m" not in result, (
                f"Private param byte '{char}' tail survived cleaning in: {result!r}"
            )
            assert "\x1b" not in result, (
                f"ESC byte survived cleaning for param byte '{char}' in: {result!r}"
            )
            assert "text" in result

    def test_yaml_parse_succeeds_after_cleaning_private_ansi(self):
        """YAML parse must succeed on output that contains ESC[>4m wrappers."""
        raw = "\x1b[>4mlifecycle:\n  status: active\n\x1b[>4m"
        cleaned = _clean_claude_output(raw)
        parsed = yaml.safe_load(cleaned)
        assert parsed["lifecycle"]["status"] == "active"

    def test_clean_claude_output_existing_sequences_still_work(self):
        """Regression: existing CSI sequences (ESC[0m, ESC[1;32m, ESC[?25h) still stripped."""
        output = "\x1b[0mtext\x1b[1;32mgreen\x1b[?25hcursor"
        result = _clean_claude_output(output)
        assert "\x1b" not in result
        assert "textgreencursor" in result


class TestInvokeClaudeCliNoColor:
    """Tests for Bug #850: NO_COLOR=1 must be set to prevent ANSI at source."""

    def test_invoke_claude_cli_sets_no_color_env(self, tmp_path):
        """invoke_claude_cli must pass NO_COLOR=1 in the subprocess env dict."""
        captured_env = {}

        def fake_subprocess_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "---\nlifecycle:\n  status: active\n"
            mock_result.stderr = ""
            return mock_result

        with patch("subprocess.run", side_effect=fake_subprocess_run):
            invoke_claude_cli(
                repo_path=str(tmp_path),
                prompt="describe this repo",
                shell_timeout_seconds=_SHELL_TIMEOUT_SECONDS,
                outer_timeout_seconds=_OUTER_TIMEOUT_SECONDS,
            )

        assert captured_env.get("NO_COLOR") == "1", (
            f"NO_COLOR=1 not found in subprocess env. Got: {captured_env.get('NO_COLOR')!r}"
        )


class TestCleanClaudeOutputEcma48IntermediateBytes:
    """Tests for full ECMA-48 CSI grammar: intermediate bytes and non-letter final bytes."""

    def test_clean_claude_output_strips_csi_with_intermediate_byte_space(self):
        """ESC[1 q (intermediate byte space, ECMA-48 cursor shape command) must be stripped."""
        output = "a\x1b[1 qb"
        result = _clean_claude_output(output)
        assert "\x1b" not in result, f"ESC byte survived cleaning in: {result!r}"
        assert "1 q" not in result, (
            f"CSI sequence tail survived cleaning in: {result!r}"
        )
        assert "ab" in result, f"Surrounding text must be preserved, got: {result!r}"

    def test_clean_claude_output_strips_csi_with_final_byte_tilde(self):
        """ESC[1~ (final byte tilde, VT function key sequence) must be stripped."""
        output = "a\x1b[1~b"
        result = _clean_claude_output(output)
        assert "\x1b" not in result, f"ESC byte survived cleaning in: {result!r}"
        assert "1~" not in result, f"CSI sequence tail survived cleaning in: {result!r}"
        assert "ab" in result, f"Surrounding text must be preserved, got: {result!r}"

    def test_clean_claude_output_strips_csi_with_intermediate_byte_dollar(self):
        """ESC[1$z (intermediate byte dollar) must be stripped."""
        output = "a\x1b[1$zb"
        result = _clean_claude_output(output)
        assert "\x1b" not in result, f"ESC byte survived cleaning in: {result!r}"
        assert "1$z" not in result, (
            f"CSI sequence tail survived cleaning in: {result!r}"
        )
        assert "ab" in result, f"Surrounding text must be preserved, got: {result!r}"


class TestBuildClaudeEnvNoColor:
    """Tests for Bug #850: _build_claude_env in description_refresh_scheduler must set NO_COLOR=1."""

    def test_build_claude_env_sets_no_color(self):
        """_build_claude_env must include NO_COLOR=1 to suppress ANSI at the subprocess source."""
        from code_indexer.server.services.description_refresh_scheduler import (
            _build_claude_env,
        )

        env = _build_claude_env()
        assert env.get("NO_COLOR") == "1", (
            f"NO_COLOR=1 not found in _build_claude_env result. Got: {env.get('NO_COLOR')!r}"
        )
