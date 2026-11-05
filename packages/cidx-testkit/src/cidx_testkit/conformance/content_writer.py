"""Conformance suite for the content writer seam (``cidx_sdk.seams.content_writer``).

Subclass :class:`ContentWriterContract` and provide ``writer_subject``: the
writer under test, the ABSOLUTE root it is addressed with, and the directory
where that root's files really are (they differ for a remote service, which
maps the roots it is asked about onto its own store)::

    class TestLocalContentWriter(ContentWriterContract):
        @pytest.fixture
        def writer_subject(self, tmp_path):
            repo = tmp_path / "repo"
            repo.mkdir()
            return WriterSubject(LocalContentWriter(), str(repo), repo)

Failures are checked by their ``code`` (``writer_error_code``), never by
exception type: a delegate raises its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from cidx_sdk.seams.content_identity import git_blob_hash_bytes
from cidx_sdk.seams.content_writer import (
    ECONFLICT,
    EFORBIDDEN,
    EINVAL,
    ContentWriter,
    writer_error_code,
)


@dataclass
class WriterSubject:
    """A writer under test and the repository it writes.

    Attributes:
        writer: the implementation under test.
        root: the repository's absolute path as the WRITER is addressed.
        directory: where the repository's files are on this machine.
    """

    writer: ContentWriter
    root: str
    directory: Path

    def path(self, rel: str) -> str:
        """The absolute path the writer takes for *rel*."""
        return self.root.rstrip("/") + "/" + rel


def _code(caught: pytest.ExceptionInfo) -> object:
    return writer_error_code(caught.value)


class ContentWriterContract:
    """The content writer's contract. Subclass and provide ``writer_subject``."""

    @pytest.fixture
    def writer_subject(self, tmp_path: Path) -> WriterSubject:
        raise NotImplementedError("provide the writer under test")

    def test_satisfies_the_protocol(self, writer_subject: WriterSubject) -> None:
        assert isinstance(writer_subject.writer, ContentWriter)

    def test_put_writes_the_bytes_and_returns_their_blob_sha(
        self, writer_subject: WriterSubject
    ) -> None:
        sha = writer_subject.writer.put(writer_subject.path("docs/new.md"), b"one\n")
        assert sha == git_blob_hash_bytes(b"one\n")
        assert (writer_subject.directory / "docs/new.md").read_bytes() == b"one\n"

    def test_an_unconditional_put_overwrites(
        self, writer_subject: WriterSubject
    ) -> None:
        path = writer_subject.path("a.txt")
        writer_subject.writer.put(path, b"one")
        writer_subject.writer.put(path, b"two")
        assert (writer_subject.directory / "a.txt").read_bytes() == b"two"

    def test_none_means_must_not_exist(self, writer_subject: WriterSubject) -> None:
        path = writer_subject.path("a.txt")
        writer_subject.writer.put(path, b"one", expected_blob_sha=None)
        with pytest.raises(Exception) as caught:
            writer_subject.writer.put(path, b"two", expected_blob_sha=None)
        assert _code(caught) == ECONFLICT
        assert (writer_subject.directory / "a.txt").read_bytes() == b"one"

    def test_a_sha_must_match_the_current_blob(
        self, writer_subject: WriterSubject
    ) -> None:
        path = writer_subject.path("a.txt")
        first = writer_subject.writer.put(path, b"one")
        assert isinstance(first, str)
        with pytest.raises(Exception) as caught:
            writer_subject.writer.put(
                path, b"two", expected_blob_sha=git_blob_hash_bytes(b"other")
            )
        assert _code(caught) == ECONFLICT
        writer_subject.writer.put(path, b"two", expected_blob_sha=first)
        assert (writer_subject.directory / "a.txt").read_bytes() == b"two"

    def test_a_sha_for_an_absent_file_conflicts(
        self, writer_subject: WriterSubject
    ) -> None:
        with pytest.raises(Exception) as caught:
            writer_subject.writer.put(
                writer_subject.path("absent.txt"),
                b"x",
                expected_blob_sha=git_blob_hash_bytes(b"x"),
            )
        assert _code(caught) == ECONFLICT
        assert not (writer_subject.directory / "absent.txt").exists()

    def test_delete_reports_whether_there_was_a_file(
        self, writer_subject: WriterSubject
    ) -> None:
        path = writer_subject.path("a.txt")
        writer_subject.writer.put(path, b"one")
        assert writer_subject.writer.delete(path) is True
        assert not (writer_subject.directory / "a.txt").exists()
        assert writer_subject.writer.delete(path) is False

    def test_a_conditional_delete_checks_the_blob(
        self, writer_subject: WriterSubject
    ) -> None:
        path = writer_subject.path("a.txt")
        sha = writer_subject.writer.put(path, b"one")
        assert isinstance(sha, str)
        with pytest.raises(Exception) as caught:
            writer_subject.writer.delete(
                path, expected_blob_sha=git_blob_hash_bytes(b"other")
            )
        assert _code(caught) == ECONFLICT
        assert writer_subject.writer.delete(path, expected_blob_sha=sha) is True

    def test_git_metadata_is_never_written(self, writer_subject: WriterSubject) -> None:
        with pytest.raises(Exception) as caught:
            writer_subject.writer.put(writer_subject.path(".git/config"), b"x")
        assert _code(caught) == EFORBIDDEN

    def test_a_relative_path_is_refused(self, writer_subject: WriterSubject) -> None:
        with pytest.raises(Exception) as caught:
            writer_subject.writer.put("relative/a.txt", b"x")
        assert _code(caught) == EINVAL

    def test_a_symlink_is_not_written_through(
        self, writer_subject: WriterSubject
    ) -> None:
        (writer_subject.directory / "target.txt").write_bytes(b"kept")
        os.symlink("target.txt", writer_subject.directory / "link.txt")
        with pytest.raises(Exception) as caught:
            writer_subject.writer.put(writer_subject.path("link.txt"), b"x")
        assert _code(caught) == EFORBIDDEN
        assert (writer_subject.directory / "target.txt").read_bytes() == b"kept"
