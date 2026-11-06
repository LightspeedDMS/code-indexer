"""Reproduce a text-mode read from bytes already in hand.

The chunkers open each file in TEXT mode and try a ladder of encodings. Once the
bytes arrive from a content backend rather than from ``open()``, that read has to
be reproduced exactly -- and "decode the bytes" is not the same thing, in two ways
that both silently change the indexed text:

* **Universal newlines.** ``open(path, "r")`` translates ``\\r\\n`` and a lone
  ``\\r`` to ``\\n``. A plain ``bytes.decode()`` does not, so every CRLF file would
  chunk differently.
* **The encoding ladder is ordered, and ``utf-8`` comes first.** A file with a
  UTF-8 BOM decodes fine as ``utf-8``, keeping the BOM as ``\\ufeff``; it is
  ``utf-8-sig`` that strips it. Trying the ladder in a different order, or
  stopping at the first "looks valid", changes the text of every BOM file.

Neither raises. Both change chunk text, and chunk text feeds the deterministic
point UUID of every chunk -- so a mismatch RE-IDENTIFIES chunks rather than
invalidating them, leaving duplicate content behind in the index.
"""

from __future__ import annotations

from typing import Optional, Sequence

#: The ladder the chunkers use, in order. Kept here so both chunkers and any
#: backend-fed read agree on it rather than each carrying a copy.
DEFAULT_ENCODINGS = ("utf-8", "utf-8-sig", "latin-1", "cp1252")


def decode_like_text_mode(
    data: bytes, encodings: Sequence[str] = DEFAULT_ENCODINGS
) -> Optional[str]:
    """Decode ``data`` exactly as ``open(path, "r", encoding=...).read()`` would.

    Tries the encodings in order and returns the first that decodes, with
    universal-newline translation applied. Returns ``None`` when none of them
    decode, which is what the chunkers treat as "cannot read this file".
    """
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        # Order matters: translating a lone "\r" first would turn "\r\n" into
        # "\n\n" and silently double every line break in a CRLF file.
        return text.replace("\r\n", "\n").replace("\r", "\n")
    return None
