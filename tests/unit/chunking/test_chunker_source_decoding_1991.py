"""Bug #1991: indexing decode sites share one helper with retrieval.

Characterization: after routing the chunkers and FileFinder through
``code_indexer.utils.source_text_decoding`` the produced chunks must be
identical to what the pre-#1991 inline text-mode read produced, otherwise
already-indexed line ranges would stop matching retrieval.
"""

from pathlib import Path

import pytest

from code_indexer.config import Config, IndexingConfig
from code_indexer.indexing.chunker import TextChunker
from code_indexer.indexing.file_finder import FileFinder
from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker


def _legacy_text_mode_read(path: Path) -> str:
    for encoding in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
        try:
            with open(path, "r", encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise AssertionError("legacy loop could not decode")


def _body(prefix: bytes, sep: bytes) -> bytes:
    lines = [prefix + b"header"]
    for i in range(80):
        lines.append(b"    call_%d(); // r\xe9sum\xe9\x85 %d" % (i, i))
    return sep.join(lines) + sep


SAMPLES = {
    "latin1_lf": _body(b"// Caf\xe9 ", b"\n"),
    "cp1252_crlf_lone_cr": _body(b"// x ", b"\r\n") + b"a\rb\r",
    "utf8_bom_crlf": b"\xef\xbb\xbf" + "// café\r\nfn main() {}\r\n".encode() * 60,
    "utf8_plain": "// café   sep\x0c ff\n".encode("utf-8") * 80,
}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_fixed_size_chunker_matches_legacy_decode(tmp_path, name):
    path = tmp_path / f"{name}.cs"
    path.write_bytes(SAMPLES[name])
    chunker = FixedSizeChunker(IndexingConfig())

    expected = chunker.chunk_text(_legacy_text_mode_read(path), path, tmp_path)
    assert chunker.chunk_file(path, repo_root=tmp_path) == expected


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_text_chunker_matches_legacy_decode(tmp_path, name):
    path = tmp_path / f"{name}.cs"
    path.write_bytes(SAMPLES[name])
    chunker = TextChunker(IndexingConfig(chunk_size=500, chunk_overlap=50))

    expected = chunker.chunk_text(_legacy_text_mode_read(path), path, repo_root=None)
    assert chunker.chunk_file(path) == expected


def _finder(tmp_path: Path) -> FileFinder:
    return FileFinder(Config(codebase_dir=tmp_path))


def test_file_finder_sniff_accepts_latin1_unknown_extension(tmp_path):
    path = tmp_path / "LEGACY.unknownext"
    path.write_bytes(b"caf\xe9 \x85 text\r\n" * 10)
    assert _finder(tmp_path)._is_text_file(path) is True


def test_file_finder_sniff_rejects_null_bytes_and_empty(tmp_path):
    binary = tmp_path / "blob.unknownext"
    binary.write_bytes(b"abc\x00def")
    empty = tmp_path / "empty.unknownext"
    empty.write_bytes(b"")
    finder = _finder(tmp_path)
    assert finder._is_text_file(binary) is False
    assert finder._is_text_file(empty) is False
