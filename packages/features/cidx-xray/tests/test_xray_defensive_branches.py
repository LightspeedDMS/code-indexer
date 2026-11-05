"""Coverage-gap tests that exercise defensive branches via mock injection.

Separate from test_encoding_edge_cases.py to keep both files under MESSI Rule 6
(500 lines).  Uses MagicMock / sys.modules patching to force specific source-coverage
branches in ast_engine, languages, and xray_node that cannot be reached with real
inputs alone.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Coverage gap tests — hit specific uncovered lines in source modules
# ---------------------------------------------------------------------------


class TestXRayDefensiveBranches:
    """Targeted tests to hit uncovered branches in ast_engine and xray_node."""

    def test_hcl_available_returns_true_when_module_injected(self) -> None:
        """languages.py line 57: _hcl_available() returns True when tree_sitter_hcl is present.

        Injects a fake tree_sitter_hcl into sys.modules to trigger the True branch.
        The real _hcl_available() function is called — not mocked.
        """
        # Inject a fake tree_sitter_hcl before calling _hcl_available
        fake_hcl = MagicMock()
        sys.modules["tree_sitter_hcl"] = fake_hcl

        # Force reimport of languages so it picks up the injected module
        saved_lang_mod = sys.modules.pop("cidx_xray.languages", None)
        try:
            from cidx_xray.languages import _hcl_available

            result = _hcl_available()
            assert result is True, (
                "_hcl_available() should return True when tree_sitter_hcl is importable"
            )
        finally:
            del sys.modules["tree_sitter_hcl"]
            # Restore languages module
            if saved_lang_mod is not None:
                sys.modules["cidx_xray.languages"] = saved_lang_mod
            elif "cidx_xray.languages" in sys.modules:
                del sys.modules["cidx_xray.languages"]

    def test_xray_node_text_returns_empty_string_when_raw_is_none(self) -> None:
        """xray_node.py line 45: .text returns '' when _node.text is None.

        Constructs a fake node whose .text attribute is None to trigger the
        defensive guard. This covers the None-safety path.
        """
        from cidx_xray.xray_node import XRayNode

        fake_node = MagicMock()
        fake_node.text = None
        # Other attributes needed by XRayNode properties
        fake_node.children = []

        node = XRayNode(fake_node)
        result = node.text
        assert result == "", f"Expected '' when _node.text is None, got {result!r}"
        assert isinstance(result, str)

    def test_xray_node_text_returns_str_when_raw_is_str(self) -> None:
        """xray_node.py line 49: .text returns str(raw) when _node.text is already a str.

        Some tree-sitter versions return str directly from .text. The wrapper
        must handle both bytes and str cases.
        """
        from cidx_xray.xray_node import XRayNode

        fake_node = MagicMock()
        fake_node.text = "already a string"
        fake_node.children = []

        node = XRayNode(fake_node)
        result = node.text
        assert result == "already a string"
        assert isinstance(result, str)
