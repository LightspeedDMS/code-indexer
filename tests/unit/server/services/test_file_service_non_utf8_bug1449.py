"""Regression tests for GitHub Bug #1449.

get_file_content / get_file_content_by_path must not raise UnicodeDecodeError
when a repository file contains non-UTF-8 bytes (e.g. a Windows-1252 en-dash
0x96). Both read sites in FileListingService open files with strict
encoding="utf-8" and no error policy, so f.readlines() raises on any byte
sequence that is not valid UTF-8 -- even though the file genuinely exists and
should be readable.

Expected fix: open with errors="replace" so invalid bytes are substituted
with U+FFFD instead of raising, keeping line/offset pagination byte-stable.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.config_service import reset_config_service
from code_indexer.server.services.file_service import FileListingService


# Windows-1252 byte 0x96 (en-dash) is not valid standalone UTF-8 -- this is
# the exact byte reported in the bug's log evidence.
NON_UTF8_BYTE = b"\x96"

BAD_FILE_RAW = (
    b"line one is normal ascii\n"
    b"line two has a bad byte here" + NON_UTF8_BYTE + b" right there\n"
    b"line three is normal again\n"
)
GOOD_FILE_TEXT = "line one\nline two\nline three\n"
EXPECTED_LINE_COUNT = 3


@pytest.fixture
def repo_with_non_utf8_file():
    """Temp repo containing one non-UTF-8 file and one plain UTF-8 file.

    Also wires CIDX_SERVER_DATA_DIR to an isolated temp config dir so
    get_config_service() does not touch real server config, matching the
    pattern used by the sibling skip_truncation test suite.
    """
    temp_dir = tempfile.mkdtemp()
    repo_path = Path(temp_dir) / "test_repo"
    repo_path.mkdir(parents=True)

    with open(repo_path / "bad_encoding.py", "wb") as f:
        f.write(BAD_FILE_RAW)
    with open(repo_path / "good_encoding.py", "w", encoding="utf-8") as f:
        f.write(GOOD_FILE_TEXT)

    original_env = os.environ.get("CIDX_SERVER_DATA_DIR")
    config_dir = Path(temp_dir) / "cidx_config"
    config_dir.mkdir(parents=True)
    os.environ["CIDX_SERVER_DATA_DIR"] = str(config_dir)
    reset_config_service()

    yield repo_path

    reset_config_service()
    if original_env is not None:
        os.environ["CIDX_SERVER_DATA_DIR"] = original_env
    else:
        os.environ.pop("CIDX_SERVER_DATA_DIR", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_get_file_content_by_path_non_utf8_does_not_raise(repo_with_non_utf8_file):
    """get_file_content_by_path: bad byte must not raise UnicodeDecodeError."""
    service = FileListingService()

    # Before the fix, this call raises UnicodeDecodeError.
    result = service.get_file_content_by_path(
        repo_path=str(repo_with_non_utf8_file),
        file_path="bad_encoding.py",
    )

    content = result["content"]
    assert "�" in content
    assert "line one is normal ascii" in content
    assert "line three is normal again" in content
    assert result["metadata"]["total_lines"] == EXPECTED_LINE_COUNT


def test_get_file_content_by_path_utf8_happy_path_unaffected(
    repo_with_non_utf8_file,
):
    """Existing normal-UTF-8 behavior must be unchanged by the fix."""
    service = FileListingService()

    result = service.get_file_content_by_path(
        repo_path=str(repo_with_non_utf8_file),
        file_path="good_encoding.py",
    )

    assert result["content"] == GOOD_FILE_TEXT
    assert "�" not in result["content"]
    assert result["metadata"]["total_lines"] == EXPECTED_LINE_COUNT


def test_get_file_content_non_utf8_does_not_raise(repo_with_non_utf8_file):
    """get_file_content (alias path): bad byte must not raise (matches log evidence)."""
    # Bypass __init__ wiring (DB-backed activated_repo_manager) and stub only
    # the repo-path lookup indirection -- the code under test here is the
    # file-read/decoding logic, not repository resolution.
    service = FileListingService.__new__(FileListingService)
    arm = MagicMock()
    arm.get_activated_repo_path.return_value = str(repo_with_non_utf8_file)
    service.activated_repo_manager = arm  # type: ignore[attr-defined]

    # Before the fix, this call raises UnicodeDecodeError.
    result = service.get_file_content(
        repository_alias="myrepo",
        file_path="bad_encoding.py",
        username="testuser",
    )

    content = result["content"]
    assert "�" in content
    assert "line one is normal ascii" in content
    assert "line three is normal again" in content
    assert result["metadata"]["total_lines"] == EXPECTED_LINE_COUNT
