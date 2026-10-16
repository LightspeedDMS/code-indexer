"""Tests for cli._parse_file_extensions (Story #906, #2047).

Every comma-separated token of a non-empty value is validated by the shared
#2047 rule (services/extension_filter.py): stripped, lowercased, ONE
leading dot dropped. A blank token, or one that can never be a file suffix,
raises the same ValueError every other door raises -- nothing is silently
dropped. An absent flag or a wholly empty value is no filter (documented in
the --file-extensions help). End-to-end coverage:
tests/unit/cli/test_file_extensions_or_2047.py.
"""

import pytest

from code_indexer.cli import _parse_file_extensions
from code_indexer.services.extension_filter import normalize_extensions


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, []),
        ("", []),
        ("   ", []),
        ("py", ["py"]),
        ("py,js", ["py", "js"]),
        ("py,js,ts", ["py", "js", "ts"]),
        (".py", ["py"]),
        (".py,.JS", ["py", "js"]),
        ("py, js, ts", ["py", "js", "ts"]),
        ("  py  ,  js  ", ["py", "js"]),
        (" .py , .js , ts ", ["py", "js", "ts"]),
        ("c++", ["c++"]),
    ],
)
def test_parse_file_extensions(raw, expected):
    assert _parse_file_extensions(raw) == expected


@pytest.mark.parametrize(
    "raw, bad_token",
    [
        (".", "."),
        ("py, ", " "),
        (",,,", ""),
        ("py,,js", ""),
        (".,py", "."),
        ("..py", "..py"),
        ("py,tar.gz", "tar.gz"),
    ],
)
def test_invalid_token_raises_the_shared_error(raw, bad_token):
    with pytest.raises(ValueError) as cli_error:
        _parse_file_extensions(raw)
    with pytest.raises(ValueError) as shared_error:
        normalize_extensions([bad_token])
    assert str(cli_error.value) == str(shared_error.value)
