"""Bug #1991: one shared decode helper for indexing AND query-time retrieval.

Retrieval must decode a source file exactly the way indexing decoded it, so
the helper is pinned against the indexer's historical behaviour: a text-mode
``open(path, encoding=enc).read()`` tried over the fallback encodings, which
also applies universal-newline translation (``\\r\\n`` and lone ``\\r`` become
``\\n``) and nothing else.
"""

from pathlib import Path

import pytest

from code_indexer.utils.source_text_decoding import (
    SOURCE_TEXT_ENCODINGS,
    decode_source_bytes,
    decode_source_text,
    read_source_text,
    split_source_lines,
)


def _legacy_text_mode_read(path: Path) -> str:
    """The indexer's pre-#1991 read loop, kept verbatim as the oracle."""
    for encoding in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
        try:
            with open(path, "r", encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise AssertionError("legacy loop could not decode")


SAMPLES = {
    "utf8_plain": "def café():\n    return 1\n".encode("utf-8"),
    "utf8_bom": b"\xef\xbb\xbfclass A {}\r\n",
    "latin1": "/// Caf\xe9 image generator\nclass G {}\n".encode("latin-1"),
    "cp1252_ellipsis_crlf": b"// wait\x85 here\r\nline two\r\nthree\rfour\n",
    "utf8_formfeed_nel": "a\x0cb\nc\u0085d\ne f\n".encode("utf-8"),
    "empty": b"",
}


def test_fallback_order_matches_indexer():
    assert SOURCE_TEXT_ENCODINGS == ("utf-8", "utf-8-sig", "latin-1", "cp1252")


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_decode_matches_legacy_text_mode_read(tmp_path, name):
    path = tmp_path / f"{name}.txt"
    path.write_bytes(SAMPLES[name])

    assert decode_source_text(SAMPLES[name]) == _legacy_text_mode_read(path)
    assert read_source_text(path) == _legacy_text_mode_read(path)


def test_decode_source_bytes_does_not_touch_newlines():
    assert decode_source_bytes(b"a\r\nb\rc") == "a\r\nb\rc"
    assert decode_source_bytes(b"caf\xe9") == "caf\xe9"


def test_split_source_lines_splits_on_newline_only():
    text = "a\x0cb\nc\x85d\ne f\x0bg\nlast"
    assert split_source_lines(text) == ["a\x0cb\n", "c\x85d\n", "e f\x0bg\n", "last"]


def test_split_source_lines_matches_readlines_for_normalised_text(tmp_path):
    path = tmp_path / "x.txt"
    path.write_bytes(SAMPLES["cp1252_ellipsis_crlf"])
    with open(path, encoding="latin-1") as f:
        expected = f.readlines()
    assert split_source_lines(read_source_text(path)) == expected


def test_split_source_lines_empty_text():
    assert split_source_lines("") == []


def test_decode_raises_when_no_encoding_decodes(monkeypatch):
    import code_indexer.utils.source_text_decoding as decoding

    monkeypatch.setattr(decoding, "SOURCE_TEXT_ENCODINGS", ("utf-8",))
    with pytest.raises(ValueError, match="Could not decode"):
        decoding.decode_source_bytes(b"caf\xe9")
