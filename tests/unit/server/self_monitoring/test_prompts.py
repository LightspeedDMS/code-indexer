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

    # --- AC1: Frequency-Based Warning Escalation ---

    def test_prompt_contains_repeating_warning_detection_section(self):
        """AC1: Prompt must have a dedicated 'Repeating Warning Detection' section."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        assert "Repeating Warning Detection" in prompt

    def test_repeating_warning_section_placed_before_ignore_list(self):
        """AC1: Repeating Warning Detection section must appear BEFORE the ignore list."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        repeating_pos = prompt.find("Repeating Warning Detection")
        ignore_pos = prompt.find("DO NOT CREATE ISSUES FOR")

        assert repeating_pos != -1, "Repeating Warning Detection section not found"
        assert ignore_pos != -1, "DO NOT CREATE ISSUES FOR section not found"
        assert repeating_pos < ignore_pos, (
            "Repeating Warning Detection section must appear BEFORE the ignore list"
        )

    def test_prompt_contains_escalation_threshold(self):
        """AC1: Prompt must state the 5+ occurrence escalation threshold."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        # Must include the escalation threshold of 5 occurrences
        assert "5" in prompt
        # Must indicate that threshold triggers evaluation as potential unrecoverable state
        assert "unrecoverable" in prompt

    def test_prompt_contains_repeating_warning_classification_as_server_bug(self):
        """AC1: Repeating warnings must be classified as server_bug."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        # Must instruct to classify repeating warnings as server_bug
        # with the "Repeating warning:" title prefix
        assert "Repeating warning" in prompt

    def test_prompt_states_frequency_overrides_ignore_list(self):
        """AC1: Prompt must explicitly state frequency escalation overrides the ignore list."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        # Must explicitly state that high-frequency warnings override the ignore list
        assert "overrides" in prompt or "override" in prompt

    # --- AC2: SQL Query Template ---

    def test_prompt_contains_sql_query_for_frequency_detection(self):
        """AC2: Prompt must contain a concrete SQL query for detecting repeating warnings."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        # Must contain the SQL query with SUBSTR grouping on message
        assert "SUBSTR(message, 1, 80)" in prompt

    def test_sql_query_groups_by_message_pattern_and_source(self):
        """AC2: SQL query must GROUP BY message pattern and source."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        assert "GROUP BY SUBSTR(message, 1, 80), source" in prompt

    def test_sql_query_has_having_count_threshold(self):
        """AC2: SQL query must filter with HAVING COUNT(*) >= 5."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        assert "HAVING COUNT(*) >= 5" in prompt

    def test_sql_query_filters_by_warning_level(self):
        """AC2: SQL query must filter for WARNING level entries."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        assert "level = 'WARNING'" in prompt

    def test_sql_query_uses_last_scan_log_id_placeholder(self):
        """AC2: SQL query must use {last_scan_log_id} placeholder for delta processing."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        assert "{last_scan_log_id}" in prompt

    # --- AC3: Classification Conflict Resolution ---

    def test_ignore_list_has_frequency_qualifier(self):
        """AC3: Ignore list must include frequency qualifier (fewer than 5 times)."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        # The ignore list entry for "Expected warnings" must be qualified with frequency limit
        # Must mention "fewer than 5" or "less than 5" as the qualifier
        assert "fewer than 5" in prompt or "less than 5" in prompt

    def test_frequency_detection_runs_before_ignore_list(self):
        """AC3: Priority chain must be explicit - frequency detection before ignore list."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        repeating_section_pos = prompt.find("Repeating Warning Detection")
        ignore_list_pos = prompt.find("DO NOT CREATE ISSUES FOR")

        assert repeating_section_pos < ignore_list_pos, (
            "Frequency detection section must come before the ignore list"
        )

    # --- AC4: Stuck-State Warning Examples ---

    def test_prompt_contains_git_packfile_corruption_example(self):
        """AC4: Prompt must include corrupted git packfile as a stuck-state example."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        # Must mention git packfile corruption as an example of stuck-state warning
        assert "pack" in prompt.lower() and ("delta" in prompt.lower() or "packfile" in prompt.lower())

    def test_prompt_contains_daemon_socket_example(self):
        """AC4: Prompt must include dead daemon socket as a stuck-state example."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        assert "daemon socket" in prompt.lower() or "daemon" in prompt.lower()

    def test_prompt_contains_stale_lock_file_example(self):
        """AC4: Prompt must include stale lock file as a stuck-state example."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        assert "lock" in prompt.lower()

    def test_prompt_contains_stuck_state_examples_table_or_list(self):
        """AC4: Prompt must present stuck-state examples in a structured format."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        # Must have examples of patterns that indicate unrecoverable states
        assert "self-heal" in prompt.lower() or "not self-heal" in prompt.lower() or "will not self" in prompt.lower()

    # --- AC5: Prompt Version Tracking ---

    def test_prompt_has_version_comment_at_top(self):
        """AC5: Prompt must have a version comment at the top."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        # Version comment must be present
        assert "Prompt version:" in prompt or "Prompt version: 2" in prompt

    def test_prompt_version_comment_is_near_top(self):
        """AC5: Version comment must be near the top of the file (within first 300 chars)."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        version_pos = prompt.find("Prompt version:")
        assert version_pos != -1, "Prompt version comment not found"
        assert version_pos < 300, (
            f"Version comment must be within first 300 chars, found at position {version_pos}"
        )

    def test_prompt_version_is_2(self):
        """AC5: Prompt version must be 2 after this update."""
        from code_indexer.server.self_monitoring.prompts import get_default_prompt

        prompt = get_default_prompt()

        assert "Prompt version: 2" in prompt
