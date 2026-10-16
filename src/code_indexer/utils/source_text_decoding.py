"""Single source of truth for decoding source files (Bug #1991).

Indexing and query-time content retrieval MUST decode a file identically:
the chunker computes ``line_start``/``line_end`` from the decoded text, and
retrieval later slices those same lines back out. Any divergence (strict
UTF-8 at query time, ``str.splitlines()`` breaking on extra separators)
returns the wrong lines or fails outright for non-UTF-8 files.

The behaviour reproduces the indexer's historical text-mode read
``open(path, "r", encoding=enc).read()`` over :data:`SOURCE_TEXT_ENCODINGS`:
the first encoding that decodes strictly wins, then universal-newline
translation turns ``\\r\\n`` and lone ``\\r`` into ``\\n`` (and nothing else).
"""

import io
from pathlib import Path
from typing import List

# Fallback order used by indexing since before #1991. latin-1 maps every byte,
# so decoding never fails once it is reached; the order is kept verbatim so
# already-indexed line ranges keep matching.
SOURCE_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "latin-1", "cp1252")


def decode_source_bytes(data: bytes) -> str:
    """Decode raw file bytes with the first encoding that succeeds strictly.

    Newlines are NOT translated; see :func:`decode_source_text`.
    """
    for encoding in SOURCE_TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode source bytes with any supported encoding")


def decode_source_text(data: bytes) -> str:
    """Decode file bytes exactly as the indexer's text-mode read does."""
    return decode_source_bytes(data).replace("\r\n", "\n").replace("\r", "\n")


def read_source_text(path: Path) -> str:
    """Read and decode a source file exactly as indexing does."""
    with open(path, "rb") as f:
        return decode_source_text(f.read())


def split_source_lines(text: str) -> List[str]:
    """Split decoded text into lines on ``\\n`` only, keeping line endings.

    Matches ``TextIOWrapper.readlines()`` on already-normalised text and the
    chunker's ``text.count("\\n")`` line arithmetic. Never use
    ``str.splitlines()`` here: it also breaks on ``\\x0b``, ``\\x0c``,
    ``\\x1c``-``\\x1e``, ``\\x85`` and U+2028/U+2029, which shifts every
    following line relative to the indexed line numbers.

    ``newline="\\n"`` disables newline translation and makes ``\\n`` the only
    line terminator (same cost profile as a file's ``readlines()``).
    """
    return io.StringIO(text, newline="\n").readlines()
