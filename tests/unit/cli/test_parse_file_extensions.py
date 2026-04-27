"""Tests for cli._parse_file_extensions (Story #906).

Direct unit tests of the parsing helper. These replace the prior CliRunner-spy
tests that were xfailed because Bundle 4 (#904) refactored the CLI's embedder
chain in a way the spy fixture could not cleanly mock without violating the
project's clean-code rule against module-level test seams in production code.

The parsing logic is a pure function and is the only part of #906 with
edge-case complexity worth direct testing. The threading from parsed list
into filter_conditions_list is a 4-line append loop verified by code review
at the 2 wiring sites in cli.py.

End-to-end "did the user-facing flag actually reach the vector store" coverage
is tracked as a separate follow-up integration test (real cidx subprocess
against a tiny corpus).
"""

import pytest

from code_indexer.cli import _parse_file_extensions


@pytest.mark.parametrize(
    "raw, expected",
    [
        # None / empty / whitespace inputs -> []
        (None, []),
        ("", []),
        ("   ", []),
        # Single extension
        ("py", ["py"]),
        # Multi-value comma list
        ("py,js", ["py", "js"]),
        ("py,js,ts", ["py", "js", "ts"]),
        # Leading-dot tolerance
        (".py", ["py"]),
        (".py,.js", ["py", "js"]),
        # Whitespace tolerance
        ("py, js, ts", ["py", "js", "ts"]),
        ("  py  ,  js  ", ["py", "js"]),
        # Mixed leading-dots + whitespace
        (" .py , .js , ts ", ["py", "js", "ts"]),
        # Edge cases: dot-only or dot-plus-comma normalize to empty/skipped
        (".", []),
        (".,py", ["py"]),
        ("py,.", ["py"]),
        (",,,", []),
        # Empty tokens between commas dropped
        ("py,,js", ["py", "js"]),
    ],
)
def test_parse_file_extensions(raw, expected):
    assert _parse_file_extensions(raw) == expected
