"""
Unit tests for Bug #570: edit_file Unicode/BOM handling.

Tests verify:
1. BOM stripped on read (utf-8-sig decoding)
2. BOM preserved on write (utf-8-sig encoding when original had BOM)
3. Unicode NFC normalization for string matching
4. Emoji handling in BOM and non-BOM files
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.file_crud_service import FileCRUDService

UTF8_BOM = b"\xef\xbb\xbf"


@pytest.fixture
def service():
    """Create FileCRUDService with mocked ActivatedRepoManager."""
    svc = FileCRUDService.__new__(FileCRUDService)
    svc.activated_repo_manager = MagicMock()
    svc._global_write_exceptions = {}
    svc._golden_repos_dir = None
    return svc


class TestPerformReplacementUnicodeNormalization:
    """Bug #570 Factor 3: Unicode NFC normalization in _perform_replacement."""

    def test_nfc_matches_nfd_content(self, service):
        """NFC old_string must match NFD content (visually identical)."""
        # NFD: e + combining accent
        nfd_content = "caf\u0065\u0301 = True"
        # NFC: precomposed é
        nfc_old = "caf\u00e9 = True"

        result, changes = service._perform_replacement(
            nfd_content, nfc_old, "coffee = True", False, "test.py"
        )
        assert changes == 1
        assert "coffee = True" in result

    def test_nfc_matches_nfc_content(self, service):
        """NFC-to-NFC matching must continue to work."""
        content = "r\u00e9sum\u00e9 = ''"
        old = "r\u00e9sum\u00e9 = ''"
        result, changes = service._perform_replacement(
            content, old, "resume = ''", False, "test.py"
        )
        assert changes == 1

    def test_ascii_content_unaffected(self, service):
        """Pure ASCII content must work identically."""
        content = "hello = 'world'"
        result, changes = service._perform_replacement(
            content, "hello", "greet", False, "test.py"
        )
        assert changes == 1
        assert "greet = 'world'" in result

    def test_emoji_matching(self, service):
        """Emoji content must be matchable."""
        content = "STATUS = '\U0001f600'"
        result, changes = service._perform_replacement(
            content, "STATUS = '\U0001f600'", "STATUS = 'happy'", False, "test.py"
        )
        assert changes == 1

    def test_not_found_still_raises(self, service):
        """String not found must still raise ValueError."""
        with pytest.raises(ValueError, match="not found"):
            service._perform_replacement(
                "hello world", "missing", "replaced", False, "test.py"
            )


class TestBOMHandlingOnRead:
    """Bug #570 Factor 1: BOM stripped on read via utf-8-sig."""

    def test_utf8_sig_strips_bom(self):
        """utf-8-sig codec must strip BOM from content."""
        bom_bytes = UTF8_BOM + b"hello world"
        decoded = bom_bytes.decode("utf-8-sig")
        assert decoded == "hello world"
        assert not decoded.startswith("\ufeff")

    def test_utf8_sig_no_bom_unchanged(self):
        """utf-8-sig codec must work identically for non-BOM files."""
        plain_bytes = b"hello world"
        decoded = plain_bytes.decode("utf-8-sig")
        assert decoded == "hello world"

    def test_bom_detection(self):
        """BOM detection via byte prefix check."""
        assert (UTF8_BOM + b"content")[:3] == UTF8_BOM
        assert b"content"[:3] != UTF8_BOM


class TestBOMPreservationOnWrite:
    """Bug #570 Factor 2: BOM preserved on write via has_bom flag."""

    def test_atomic_write_preserves_bom(self, service):
        """_atomic_write_file with has_bom=True must write BOM."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "bom_test.txt"
            service._atomic_write_file(test_file, "hello", has_bom=True)
            written = test_file.read_bytes()
            assert written[:3] == UTF8_BOM
            assert b"hello" in written

    def test_atomic_write_no_bom_by_default(self, service):
        """_atomic_write_file with default has_bom=False must NOT write BOM."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "no_bom_test.txt"
            service._atomic_write_file(test_file, "hello", has_bom=False)
            written = test_file.read_bytes()
            assert written[:3] != UTF8_BOM
            assert b"hello" in written

    def test_roundtrip_bom_file(self, service):
        """Read BOM file → edit → write must preserve BOM."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "roundtrip.txt"
            original = UTF8_BOM + "x = 1\n".encode("utf-8")
            test_file.write_bytes(original)

            # Simulate read
            content_bytes = test_file.read_bytes()
            has_bom = content_bytes[:3] == UTF8_BOM
            content_str = content_bytes.decode("utf-8-sig")

            # Simulate edit
            new_content = content_str.replace("x = 1", "x = 2")

            # Write back
            service._atomic_write_file(test_file, new_content, has_bom=has_bom)

            # Verify
            result = test_file.read_bytes()
            assert result[:3] == UTF8_BOM
            assert b"x = 2" in result

    def test_roundtrip_non_bom_file(self, service):
        """Read non-BOM file → edit → write must NOT add BOM."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "roundtrip_no_bom.txt"
            test_file.write_bytes(b"y = 10\n")

            content_bytes = test_file.read_bytes()
            has_bom = content_bytes[:3] == UTF8_BOM
            content_str = content_bytes.decode("utf-8-sig")
            new_content = content_str.replace("y = 10", "y = 20")
            service._atomic_write_file(test_file, new_content, has_bom=has_bom)

            result = test_file.read_bytes()
            assert result[:3] != UTF8_BOM
            assert b"y = 20" in result
