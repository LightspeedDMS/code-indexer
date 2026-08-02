"""Story #1491 AC7: MCP responses use the C JSON encoder (no indent=2).

Finding C3: json.dumps(data, indent=2) forces CPython's pure-Python
_iterencode fallback (5-10x slower, fully GIL-held) instead of the C
encoder. This test proves _mcp_response no longer requests indentation
while preserving semantic content.
"""

import json

from code_indexer.server.mcp.handlers._utils import _mcp_response


def test_mcp_response_does_not_indent_json() -> None:
    """The serialized text must not contain newline+space indentation
    artifacts that only appear when indent=2 is passed to json.dumps."""
    data = {"success": True, "results": [{"a": 1}, {"a": 2}]}
    response = _mcp_response(data)
    text = response["content"][0]["text"]

    # indent=2 always inserts "\n  " sequences for nested structures;
    # the compact C-encoder path never does.
    assert "\n" not in text


def test_mcp_response_content_is_semantically_identical() -> None:
    """Whitespace-only difference is permitted; the parsed value must be
    byte-for-byte identical to before the change."""
    data = {"success": True, "results": [1, 2, 3], "nested": {"x": "y"}}
    response = _mcp_response(data)
    text = response["content"][0]["text"]

    assert json.loads(text) == data
