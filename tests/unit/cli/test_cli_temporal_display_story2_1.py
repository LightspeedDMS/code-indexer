"""Unit tests for Story 2.1 CLI temporal display reimplementation."""

import unittest
from unittest.mock import Mock, patch

from code_indexer.cli import (
    _display_file_chunk_match,
    _display_commit_message_match,
    display_temporal_results,
)


class TestCLITemporalDisplayStory21(unittest.TestCase):
    """Test cases for Story 2.1 CLI temporal display changes."""

    def _make_result(self, **overrides):
        """Build a Mock result object for display-function tests.

        Story 2 (no SQLite): commit metadata comes from result.temporal_context,
        not from a temporal_service fetch call.
        """
        result = Mock()
        result.contributing_providers = None
        result.temporal_context = {}
        for key, value in overrides.items():
            setattr(result, key, value)
        return result

    def test_display_file_chunk_with_diff(self):
        """Test that file chunk matches display shows header, message, and content.

        NOTE: Story 2 (no SQLite) removed diff generation entirely --
        _display_file_chunk_match no longer calls temporal_service methods;
        commit metadata comes from result.temporal_context and content is
        always shown directly with line numbers, never as a diff.
        """
        result = self._make_result(
            metadata={
                "type": "file_chunk",
                "file_path": "src/auth.py",
                "line_start": 45,
                "line_end": 67,
                "commit_hash": "def5678abc123",
                "author_email": "john@example.com",
            },
            score=0.95,
            content="def validate_token(self, token):\n    return True",
            temporal_context={
                "commit_date": "2024-06-20 14:32:15",
                "author_name": "John Doe",
                "commit_message": (
                    "Fix token expiry bug in JWT validation.\n"
                    "to verify exception handling."
                ),
            },
        )
        temporal_service = Mock()  # unused by current implementation

        with patch("code_indexer.cli.console") as mock_console:
            _display_file_chunk_match(result, 1, temporal_service)
            calls = mock_console.print.call_args_list

            self.assertTrue(any("1. src/auth.py:45-67" in str(call) for call in calls))
            self.assertTrue(any("Score: 0.95" in str(call) for call in calls))
            self.assertTrue(any("Commit: def5678" in str(call) for call in calls))
            self.assertTrue(any("2024-06-20 14:32:15" in str(call) for call in calls))
            self.assertTrue(any("John Doe" in str(call) for call in calls))
            self.assertTrue(any("john@example.com" in str(call) for call in calls))
            self.assertTrue(any("Fix token expiry bug" in str(call) for call in calls))
            self.assertTrue(
                any("verify exception handling" in str(call) for call in calls)
            )
            # Content is shown directly with line numbers (no diff anymore)
            self.assertTrue(
                any(
                    "45" in str(call) and "def validate_token" in str(call)
                    for call in calls
                )
            )
            self.assertFalse(any("[DIFF" in str(call) for call in calls))

    def test_display_commit_message_match(self):
        """Test that commit message matches display with proper format.

        NOTE: Story 2 (no SQLite) removed per-file "Files Modified" listing
        entirely -- _display_commit_message_match no longer calls
        temporal_service._fetch_commit_details/_fetch_commit_file_changes; it
        always prints a fixed "tracked in diff-based index" note instead.
        """
        result = self._make_result(
            metadata={
                "type": "commit_message",
                "commit_hash": "abc1234def567",
                "author_email": "jane@example.com",
            },
            score=0.89,
            content="Add JWT validation with support for RS256 algorithm.",
            temporal_context={
                "commit_date": "2024-03-15 10:15:22",
                "author_name": "Jane Smith",
            },
        )
        temporal_service = Mock()  # unused by current implementation

        with patch("code_indexer.cli.console") as mock_console:
            _display_commit_message_match(result, 2, temporal_service)
            calls = mock_console.print.call_args_list

            self.assertTrue(
                any("[COMMIT MESSAGE MATCH]" in str(call) for call in calls)
            )
            self.assertTrue(any("Score: 0.89" in str(call) for call in calls))
            self.assertTrue(any("Commit: abc1234" in str(call) for call in calls))
            self.assertTrue(any("2024-03-15 10:15:22" in str(call) for call in calls))
            self.assertTrue(any("Jane Smith" in str(call) for call in calls))
            self.assertTrue(any("jane@example.com" in str(call) for call in calls))
            self.assertTrue(
                any("Message (matching section)" in str(call) for call in calls)
            )
            self.assertTrue(any("Add JWT validation" in str(call) for call in calls))
            self.assertTrue(
                any(
                    "File changes tracked in diff-based index" in str(call)
                    for call in calls
                )
            )

    def test_display_order_commit_messages_first(self):
        """Test that commit messages are displayed before file chunks."""
        # Create mixed results
        file_result1 = Mock()
        file_result1.metadata = {
            "type": "file_chunk",
            "file_path": "a.py",
            "line_start": 1,
            "line_end": 10,
            "commit_hash": "commit1",
            "blob_hash": "blob1",
        }
        file_result1.score = 0.99  # Higher score than commit message

        commit_result = Mock()
        commit_result.metadata = {"type": "commit_message", "commit_hash": "commit2"}
        commit_result.score = 0.85  # Lower score

        file_result2 = Mock()
        file_result2.metadata = {
            "type": "file_chunk",
            "file_path": "b.py",
            "line_start": 5,
            "line_end": 15,
            "commit_hash": "commit3",
            "blob_hash": "blob3",
        }
        file_result2.score = 0.90

        # Create results object
        results = Mock()
        results.results = [file_result1, commit_result, file_result2]  # Mixed order

        # Mock temporal service with minimal responses
        temporal_service = Mock()
        temporal_service._fetch_commit_details.return_value = {
            "hash": "test",
            "date": "2024-01-01",
            "author_name": "Test",
            "author_email": "test@example.com",
            "message": "Test",
        }
        temporal_service._fetch_commit_file_changes.return_value = []
        temporal_service._generate_chunk_diff.return_value = None

        # Mock the display functions to track call order
        with patch(
            "code_indexer.cli._display_commit_message_match"
        ) as mock_commit_display:
            with patch(
                "code_indexer.cli._display_file_chunk_match"
            ) as mock_file_display:
                display_temporal_results(results, temporal_service)

                # Verify commit message was displayed first (index 1)
                mock_commit_display.assert_called_once_with(
                    commit_result, 1, temporal_service
                )

                # Verify file chunks were displayed after (indices 2 and 3)
                calls = mock_file_display.call_args_list
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0][0], (file_result1, 2, temporal_service))
                self.assertEqual(calls[1][0], (file_result2, 3, temporal_service))

    def test_display_file_chunk_no_diff_shows_content(self):
        """Test that chunk content is shown with line numbers (no diff exists anymore)."""
        result = self._make_result(
            metadata={
                "type": "file_chunk",
                "file_path": "src/new_file.py",
                "line_start": 10,
                "line_end": 12,
                "commit_hash": "initial123",
                "author_email": "dev@example.com",
            },
            score=0.87,
            content="def new_function():\n    return True",
            temporal_context={
                "commit_date": "2024-01-01 09:00:00",
                "author_name": "Developer",
                "commit_message": "Add new file",
            },
        )
        # temporal_service is a required positional arg on the function
        # signature but is not read by the current implementation.
        temporal_service = Mock()

        with patch("code_indexer.cli.console") as mock_console:
            _display_file_chunk_match(result, 1, temporal_service)
            calls = mock_console.print.call_args_list

            # Should show content with line numbers
            self.assertTrue(
                any(
                    "10" in str(call) and "def new_function()" in str(call)
                    for call in calls
                )
            )

    def test_display_commit_message_many_files(self):
        """Test that a long, multi-line commit message displays fully.

        NOTE: Story 2 (no SQLite) removed the "Files Modified (N)" listing
        this test originally exercised entirely -- _display_commit_message_match
        always prints a fixed "tracked in diff-based index" note now,
        verified live to hold regardless of file count. Repurposed to test
        genuinely distinct current behavior: no truncation of long messages.
        """
        long_message = "\n".join(f"Change {i}: refactored module {i}" for i in range(15))
        result = self._make_result(
            metadata={
                "type": "commit_message",
                "commit_hash": "bigcommit123",
                "author_email": "refactor@example.com",
            },
            score=0.75,
            content=long_message,
            temporal_context={
                "commit_date": "2024-02-01",
                "author_name": "Refactorer",
            },
        )
        # temporal_service is a required positional arg on the function
        # signature but is not read by the current implementation.
        temporal_service = Mock()

        with patch("code_indexer.cli.console") as mock_console:
            _display_commit_message_match(result, 1, temporal_service)
            calls = mock_console.print.call_args_list

            # Full message should be shown, no truncation for any line
            for i in range(15):
                self.assertTrue(
                    any(f"Change {i}: refactored module {i}" in str(call) for call in calls)
                )
            self.assertTrue(
                any(
                    "File changes tracked in diff-based index" in str(call)
                    for call in calls
                )
            )


if __name__ == "__main__":
    unittest.main()
