"""decode_like_text_mode must match a real text-mode read, byte for byte.

These compare against `open()` itself rather than against an expectation written
by hand, because the thing being preserved is Python's behaviour, not mine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.indexing.text_decoding import DEFAULT_ENCODINGS, decode_like_text_mode


def _text_mode_read(path: Path) -> str:
    """What the chunkers do today: first encoding in the ladder that decodes."""
    for encoding in DEFAULT_ENCODINGS:
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
    raise AssertionError("fixture could not be decoded by any encoding")


@pytest.mark.parametrize(
    "name,raw",
    [
        ("lf.txt", b"a\nb\nc\n"),
        ("crlf.txt", b"a\r\nb\r\nc\r\n"),
        ("cr.txt", b"a\rb\rc"),
        ("mixed.txt", b"a\r\nb\nc\rd"),
        ("bom.txt", "﻿hello\n".encode("utf-8")),
        ("bom_crlf.txt", "﻿a\r\nb\r\n".encode("utf-8")),
        ("empty.txt", b""),
        ("unicode.txt", "café — 😀\n".encode("utf-8")),
        ("latin1.txt", b"caf\xe9\n"),
        ("no_trailing_newline.txt", b"last line"),
        ("blank_lines.txt", b"a\r\n\r\n\r\nb"),
    ],
)
def test_matches_a_real_text_mode_read(tmp_path: Path, name: str, raw: bytes) -> None:
    path = tmp_path / name
    path.write_bytes(raw)
    assert decode_like_text_mode(raw) == _text_mode_read(path)


def test_crlf_is_translated_not_doubled() -> None:
    """Translating a lone \\r first would turn \\r\\n into \\n\\n."""
    assert decode_like_text_mode(b"a\r\nb") == "a\nb"


def test_a_utf8_bom_survives_because_utf8_is_tried_before_utf8_sig() -> None:
    """utf-8 decodes a BOM file fine and KEEPS the BOM; utf-8-sig strips it.

    The ladder's order is therefore load-bearing: reordering it changes the text
    of every BOM file, and so changes those chunks' point UUIDs.
    """
    assert decode_like_text_mode("﻿hello".encode("utf-8")) == "﻿hello"
    assert decode_like_text_mode("﻿hello".encode("utf-8"), ["utf-8-sig"]) == "hello"


def test_returns_none_when_nothing_decodes() -> None:
    assert decode_like_text_mode(b"\xff\xfe\x00\x00", ["utf-8"]) is None


def test_binary_is_not_silently_accepted_as_latin1_before_utf8_is_tried() -> None:
    """latin-1 decodes ANY byte string, so it must stay last in the ladder."""
    assert DEFAULT_ENCODINGS.index("utf-8") < DEFAULT_ENCODINGS.index("latin-1")
