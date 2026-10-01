"""Unit tests for Issue #1601 AC-D2 -- _search_python_multiline's per-file
byte ceiling.

_search_python_multiline (reached when multiline=True on the grep engine,
or as ripgrep's own multiline fallback) does its own unbounded per-file
``f.read()`` -- a narrower exposure than the temp-file read sites (bounded
by single-file size, not repo-wide match volume), but the same
"read everything before deciding whether to keep it" anti-pattern.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService

_TEST_MAX_RESULTS = 100


@pytest.fixture
def service(tmp_path):
    with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
        mock_which.return_value = "/usr/bin/rg"
        return RegexSearchService(tmp_path)


class TestPythonMultilineReadBound:
    """AC-D2: a single file's content is bounded by the same byte ceiling,
    independently of the temp-file read sites' fix."""

    def test_oversized_file_is_partially_scanned_not_crashed(self, service, tmp_path):
        """A file whose content exceeds the byte ceiling must produce a
        partial scan (read_capped=True, that file's contribution to total
        treated as a lower bound), never crash/hang/corrupt."""
        import code_indexer.global_repos.regex_search as regex_search_module

        small_ceiling = 1024
        # File deliberately larger than the ceiling, containing several
        # matches for a simple multiline pattern spread across its length.
        big_content = (
            "x" * 2000 + "\nclass Foo:\n    def login(self):\n        pass\n"
        ) * 5
        (tmp_path / "big_file.py").write_text(big_content)

        with patch.object(regex_search_module, "_MAX_READ_BYTES", small_ceiling):
            matches, total = service._search_python_multiline(
                pattern=r"class[\s\S]*login",
                search_path=tmp_path,
                include_patterns=None,
                exclude_patterns=None,
                case_sensitive=True,
                max_results=_TEST_MAX_RESULTS,
            )

        # Must not crash/hang -- reaching this point already proves that.
        # The read was capped for this oversized file.
        assert service._last_search_read_capped is True
        # Priority 9 (tightened): with this exact fixture (ceiling=1024,
        # each of the 5 repeated blocks starting with 2000 'x' chars
        # before any "class Foo...login" text), the 1024-byte cutoff
        # lands entirely within the leading 'x' padding of the FIRST
        # block -- computed directly against this fixture, not a
        # tautology: zero complete matches are observable in the
        # truncated content.
        assert total == 0
        assert matches == []

    def test_oversized_multibyte_utf8_file_capped_by_true_bytes_not_characters(
        self, service, tmp_path
    ):
        """Priority 3 (Issue #1601 remediation): the per-file ceiling must
        be enforced in true BYTES, not text-mode decoded characters. Before
        this fix, ``f.read(_MAX_READ_BYTES + 1)`` on a TEXT-mode handle
        counted decoded CHARACTERS -- a UTF-8 file with many multi-byte
        characters could read up to ~4x the intended byte ceiling before
        the (character-count-based) check tripped.

        Discriminating construction: a marker string placed EXACTLY one
        byte past the true byte ceiling, preceded by enough 3-byte UTF-8
        characters to fill the ceiling exactly. A byte-correct read must
        never see the marker (it's capped to the '€' prefix alone); a
        character-counting read sees enough total content in one
        `read(N)` call to include the marker in full.
        """
        import code_indexer.global_repos.regex_search as regex_search_module

        ceiling = 300
        multibyte_char = "€"  # '€', 3 bytes in UTF-8
        prefix = multibyte_char * 100  # exactly 300 bytes
        marker = "MATCHHERE"
        content = prefix + marker + ("x" * 1000)
        (tmp_path / "multibyte.py").write_text(content, encoding="utf-8")

        # Sanity: the marker genuinely starts exactly at byte offset `ceiling`.
        encoded = content.encode("utf-8")
        assert encoded[:ceiling] == prefix.encode("utf-8")
        assert encoded[ceiling : ceiling + len(marker)] == marker.encode("utf-8")

        with patch.object(regex_search_module, "_MAX_READ_BYTES", ceiling):
            matches, total = service._search_python_multiline(
                pattern=marker,
                search_path=tmp_path,
                include_patterns=None,
                exclude_patterns=None,
                case_sensitive=True,
                max_results=_TEST_MAX_RESULTS,
            )

        assert total == 0, (
            "the marker placed exactly one byte past the true byte "
            "ceiling was found -- proves the read was bounded by decoded "
            "CHARACTER count instead of true bytes"
        )
        assert matches == []
        assert service._last_search_read_capped is True

    def test_normal_sized_file_is_unaffected(self, service, tmp_path):
        """A file comfortably under the ceiling is scanned exactly as
        before -- no read_capped, exact match count."""
        (tmp_path / "small_file.py").write_text(
            "class Foo:\n    def login(self):\n        pass\n"
        )

        matches, total = service._search_python_multiline(
            pattern=r"class[\s\S]*login",
            search_path=tmp_path,
            include_patterns=None,
            exclude_patterns=None,
            case_sensitive=True,
            max_results=_TEST_MAX_RESULTS,
        )

        assert service._last_search_read_capped is False
        assert total == 1
        assert len(matches) == 1
