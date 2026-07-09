"""
Test suite verifying critical display timing fix for daemon mode.

CONTEXT: Code review identified that daemon mode was initializing Rich Live display
INSIDE the progress callback (on first progress update), causing setup messages to
appear inline instead of scrolling at the top.

FIX: Move start_bottom_display() to BEFORE the exposed_index_blocking call, ensuring
setup messages scroll at top before progress bar appears at bottom.

ACCEPTANCE CRITERIA:
✅ Setup messages scroll at top (before progress bar appears)
✅ Progress bar pinned to bottom
✅ Display timing matches standalone behavior
✅ Concurrent files/slot tracker are extracted from kwargs and passed through
   for UX parity with standalone mode (limitation resolved; see the
   "FIX: Now extracts concurrent_files and slot_tracker from kwargs" comment
   in cli_daemon_delegation.py's progress_callback)
"""

import unittest
import re
from pathlib import Path


class TestDaemonDisplayTimingFix(unittest.TestCase):
    """Test that display initialization happens BEFORE daemon call."""

    def test_display_initialized_before_daemon_call_in_code(self):
        """
        CRITICAL: Verify start_bottom_display() is called BEFORE exposed_index_blocking().

        This code-level check ensures the fix is in place without complex mocking.
        """
        import code_indexer.cli_daemon_delegation as delegation_module

        # Read source code
        source_file = Path(delegation_module.__file__)
        source_code = source_file.read_text()

        # Find _index_via_daemon function
        match = re.search(
            r'def _index_via_daemon\(.*?\):\s*""".*?"""(.*?)^def ',
            source_code,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(match, "_index_via_daemon function not found")

        function_body = match.group(1)  # type: ignore[union-attr]

        # Find positions of key calls
        start_display_pos = function_body.find(
            "rich_live_manager.start_bottom_display()"
        )
        daemon_call_pos = function_body.find("conn.root.exposed_index_blocking(")

        # VERIFY: Both calls exist
        self.assertGreater(
            start_display_pos,
            0,
            "start_bottom_display() call not found in _index_via_daemon",
        )
        self.assertGreater(
            daemon_call_pos,
            0,
            "exposed_index_blocking() call not found in _index_via_daemon",
        )

        # VERIFY: start_display comes BEFORE daemon_call
        self.assertLess(
            start_display_pos,
            daemon_call_pos,
            "CRITICAL: start_bottom_display() must be called BEFORE exposed_index_blocking() "
            "to enable setup message scrolling at top",
        )

    def test_no_display_initialized_variable_exists(self):
        """
        Verify that display_initialized flag was properly removed.

        After the fix, we no longer need this flag since display is started early.
        """
        import code_indexer.cli_daemon_delegation as delegation_module

        # Read source code
        source_file = Path(delegation_module.__file__)
        source_code = source_file.read_text()

        # Find _index_via_daemon function
        match = re.search(
            r'def _index_via_daemon\(.*?\):\s*""".*?"""(.*?)^def ',
            source_code,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(match, "_index_via_daemon function not found")

        function_body = match.group(1)  # type: ignore[union-attr]

        # VERIFY: display_initialized variable is NOT in function
        self.assertNotIn(
            "display_initialized",
            function_body,
            "display_initialized variable should be removed after early display initialization",
        )

    def test_setup_messages_handler_in_callback(self):
        """
        Verify progress callback properly handles setup messages (total=0).

        Setup messages should go to handle_setup_message() for scrolling display.
        """
        import code_indexer.cli_daemon_delegation as delegation_module

        # Read source code
        source_file = Path(delegation_module.__file__)
        source_code = source_file.read_text()

        # Find progress_callback inside _index_via_daemon
        match = re.search(
            r'def progress_callback\(.*?\):\s*""".*?"""(.*?)(?=\n        # Map parameters|$)',
            source_code,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "progress_callback not found")

        callback_body = match.group(1)  # type: ignore[union-attr]

        # VERIFY: Setup message handling exists
        self.assertIn(
            "if total == 0:",
            callback_body,
            "Callback must check for setup messages (total=0)",
        )
        self.assertIn(
            "handle_setup_message",
            callback_body,
            "Callback must call handle_setup_message for setup messages",
        )

    def test_concurrent_files_limitation_documented(self):
        """
        Test that concurrent files / slot tracker handling in daemon mode is
        correctly implemented and documented in code.

        Historical context: daemon mode used to hardcode concurrent_files=[]
        and slot_tracker=None (a documented TODO/limitation) because it could
        not stream slot tracker data. That limitation has since been resolved:
        the RPyC callback now deserializes concurrent_files from a JSON kwarg
        (working around RPyC proxy-object caching/staleness) and extracts
        slot_tracker from kwargs, passing both through to
        progress_manager.update_complete_state() for UX parity with
        standalone mode. This test verifies THAT current behavior rather than
        the obsolete limitation (Bug #1304).
        """
        import code_indexer.cli_daemon_delegation as delegation_module

        # Read source code
        source_file = Path(delegation_module.__file__)
        source_code = source_file.read_text()

        # VERIFY: comment documents the fix and its purpose (UX parity)
        self.assertIn(
            "FIX: Now extracts concurrent_files and slot_tracker from kwargs",
            source_code,
            "Fix must be documented in code",
        )
        self.assertIn(
            "UX parity with standalone mode",
            source_code,
            "Comment should explain why concurrent files/slot tracker are extracted",
        )

        # VERIFY: concurrent_files is deserialized from the JSON kwarg, NOT hardcoded empty
        self.assertIn(
            "concurrent_files = json.loads(concurrent_files_json)",
            source_code,
            "concurrent_files must be extracted from kwargs, not hardcoded to []",
        )
        self.assertNotIn(
            "concurrent_files=[],",
            source_code,
            "concurrent_files must no longer be hardcoded to an empty list",
        )

        # VERIFY: slot_tracker is extracted from kwargs, NOT hardcoded to a None literal
        self.assertIn(
            'slot_tracker = kwargs.get("slot_tracker", None)',
            source_code,
            "slot_tracker must be extracted from kwargs",
        )
        self.assertNotIn(
            "slot_tracker=None,",
            source_code,
            "slot_tracker must no longer be hardcoded to None",
        )

        # VERIFY: both extracted values are passed through to the progress manager
        self.assertIn(
            "concurrent_files=concurrent_files,",
            source_code,
            "Extracted concurrent_files must be passed to update_complete_state",
        )
        self.assertIn(
            "slot_tracker=slot_tracker,",
            source_code,
            "Extracted slot_tracker must be passed to update_complete_state",
        )


if __name__ == "__main__":
    unittest.main()
