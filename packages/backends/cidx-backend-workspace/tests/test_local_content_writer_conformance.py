"""The local content writer passes the content writer seam's conformance suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cidx_backend_workspace.content_writer import (
    LocalContentWriter,
    make_content_writer,
)
from cidx_sdk.seams.content_writer import EINVAL, ContentWriteError
from cidx_testkit.conformance.content_writer import (
    ContentWriterContract,
    WriterSubject,
)


class TestLocalContentWriter(ContentWriterContract):
    @pytest.fixture
    def writer_subject(self, tmp_path: Path) -> WriterSubject:
        repo = tmp_path / "repo"
        repo.mkdir()
        return WriterSubject(make_content_writer(), str(repo), repo)


def test_an_overwrite_keeps_the_file_mode_and_leaves_no_temporary(
    tmp_path: Path,
) -> None:
    target = tmp_path / "script.sh"
    target.write_bytes(b"old")
    os.chmod(target, 0o751)
    LocalContentWriter().put(str(target), b"new")
    assert target.read_bytes() == b"new"
    assert target.stat().st_mode & 0o777 == 0o751
    assert [p.name for p in tmp_path.iterdir()] == ["script.sh"]


def test_a_directory_is_not_a_file_to_write(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    with pytest.raises(ContentWriteError) as caught:
        LocalContentWriter().put(str(tmp_path / "dir"), b"x")
    assert caught.value.code == EINVAL
