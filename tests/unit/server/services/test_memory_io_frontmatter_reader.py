"""memory_io.read_memory_frontmatter: bounded, body-free frontmatter reads.

Real files in tmp_path; no mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.services.memory_io import (
    MemoryFileCorruptError,
    MemoryFileNotFoundError,
    atomic_write_memory_file,
    read_memory_frontmatter,
)


def _write(tmp_path: Path, raw: bytes) -> Path:
    path = tmp_path / ("e" * 32 + ".md")
    path.write_bytes(raw)
    return path


def test_returns_frontmatter_written_by_the_memory_writer(tmp_path: Path) -> None:
    path = tmp_path / ("a" * 32 + ".md")
    atomic_write_memory_file(
        path, {"scope": "repo", "referenced_repo": "repo-a"}, "body text"
    )

    assert read_memory_frontmatter(path) == {
        "scope": "repo",
        "referenced_repo": "repo-a",
    }


def test_large_body_is_never_consumed(tmp_path: Path) -> None:
    body = b"x" * (1024 * 1024)
    path = _write(tmp_path, b"---\nscope: global\n---\n" + body)

    assert read_memory_frontmatter(path, max_bytes=1024) == {"scope": "global"}


def test_crlf_delimiters_are_accepted(tmp_path: Path) -> None:
    path = _write(tmp_path, b"---\r\nscope: global\r\n---\r\nbody\r\n")

    assert read_memory_frontmatter(path) == {"scope": "global"}


def test_empty_frontmatter_is_an_empty_mapping(tmp_path: Path) -> None:
    path = _write(tmp_path, b"---\n---\nbody\n")

    assert read_memory_frontmatter(path) == {}


def test_missing_file_raises_not_found(tmp_path: Path) -> None:
    with pytest.raises(MemoryFileNotFoundError):
        read_memory_frontmatter(tmp_path / ("b" * 32 + ".md"))


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"scope: global\n---\n", id="no-opening-delimiter"),
        pytest.param(b"", id="empty-file"),
        pytest.param(b"---\nscope: global\n", id="unterminated"),
        pytest.param(b"---\nscope: [unclosed\n---\n", id="invalid-yaml"),
        pytest.param(b"---\n- a\n- b\n---\n", id="non-mapping"),
        pytest.param(b"---\nscope: \xff\xfe\n---\n", id="not-utf8"),
    ],
)
def test_malformed_frontmatter_raises_corrupt(tmp_path: Path, raw: bytes) -> None:
    with pytest.raises(MemoryFileCorruptError):
        read_memory_frontmatter(_write(tmp_path, raw))


def test_oversized_frontmatter_raises_corrupt(tmp_path: Path) -> None:
    padding = b"summary: " + b"y" * 2048 + b"\n"
    path = _write(tmp_path, b"---\nscope: global\n" + padding + b"---\nbody\n")

    with pytest.raises(MemoryFileCorruptError):
        read_memory_frontmatter(path, max_bytes=1024)


def test_oversized_single_line_raises_corrupt(tmp_path: Path) -> None:
    path = _write(tmp_path, b"---" + b"z" * 4096)

    with pytest.raises(MemoryFileCorruptError):
        read_memory_frontmatter(path, max_bytes=1024)


def test_frontmatter_exactly_at_bound_is_read(tmp_path: Path) -> None:
    raw = b"---\nscope: global\n---\n"
    path = _write(tmp_path, raw + b"body\n")

    assert read_memory_frontmatter(path, max_bytes=len(raw)) == {"scope": "global"}


def test_rejects_non_path(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        read_memory_frontmatter(str(tmp_path / "x.md"))  # type: ignore[arg-type]


@pytest.mark.parametrize("max_bytes", [0, -1, True])
def test_rejects_non_positive_bound(tmp_path: Path, max_bytes: int) -> None:
    with pytest.raises(ValueError):
        read_memory_frontmatter(tmp_path / "x.md", max_bytes=max_bytes)
