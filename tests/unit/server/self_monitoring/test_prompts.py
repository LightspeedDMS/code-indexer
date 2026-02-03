"""
Unit tests for self-monitoring prompt templates (Bug #87).

Tests the get_default_prompt() function that loads the default analysis prompt.
"""


class TestGetDefaultPrompt:
    """Test suite for get_default_prompt() function."""

    def test_get_default_prompt_returns_string(self):
        """Test that get_default_prompt() returns a non-empty string."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_get_default_prompt_contains_required_placeholders(self):
        """Test that prompt contains required placeholders for Claude to query database directly."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        # Claude needs the database path to query it directly via sqlite3
        assert "{log_db_path}" in prompt
        assert "{last_scan_log_id}" in prompt
        assert "{dedup_context}" in prompt

    def test_get_default_prompt_loads_from_markdown_file(self):
        """Test that prompt is loaded from default_analysis_prompt.md file."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        # Verify it contains expected content from the markdown file
        assert (
            "CIDX Server Log Analysis Prompt" in prompt
            or "Log Database Access" in prompt
        )

    def test_get_default_prompt_contains_classification_instructions(self):
        """Test that prompt contains issue classification instructions."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        # Should contain the three classification types
        assert "server_bug" in prompt
        assert "client_misuse" in prompt
        assert "documentation_gap" in prompt

    def test_get_default_prompt_contains_deduplication_instructions(self):
        """Test that prompt contains three-tier deduplication section."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        # Should contain the deduplication section header
        # (actual tier instructions are injected via {dedup_context} placeholder)
        assert "Three-Tier Deduplication" in prompt
