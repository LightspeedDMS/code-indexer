"""The content seam's definition of content identity: the git blob hash.

A git blob hash is ``sha1(b"blob <bytesize>\\0" + content)``, a pure function
of the bytes. Every content backend -- local or delegated -- reports
``blob_sha`` with exactly this formula, because it seeds the deterministic
identity of every indexed chunk: a backend that computed it differently would
not fail, it would re-identify chunks and surface as duplicate content.

It reproduces ``git hash-object --no-filters``: content-rewriting
``.gitattributes`` (``text``/``eol``/``filter``) are not applied. The core's
``code_indexer.utils.blob_hash`` re-exports these functions and documents the
fleet measurement behind that choice.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict, Iterable, Optional, Union

__all__ = ["git_blob_hash", "git_blob_hash_bytes", "git_blob_hashes"]

# Read in chunks so a large file never lands in memory whole.
_CHUNK = 1024 * 1024


def _blob_digest(size: int) -> "hashlib._Hash":
    """A sha1 primed with git's blob header.

    The ONE place the header is written. Both entry points below start here, so
    the formula cannot drift between the streaming and in-memory paths -- and a
    drifting blob hash does not raise, it RE-IDENTIFIES chunks, because this value
    seeds every indexed chunk's deterministic point UUID.
    """
    digest = hashlib.sha1()
    digest.update(b"blob %d\0" % size)
    return digest


def git_blob_hash_bytes(data: bytes) -> str:
    """The git blob hash of content already in memory.

    For callers that have just read the bytes and would otherwise open the file a
    second time to hash it. That second read is not merely wasteful: the file can
    change in between, and the hash would then describe bytes the caller never
    saw. Hashing what is in hand makes the pair atomic by construction.
    """
    digest = _blob_digest(len(data))
    digest.update(data)
    return digest.hexdigest()


def git_blob_hash(path: Union[str, Path]) -> Optional[str]:
    """Return the git blob hash of ``path``, or ``None`` if it cannot be read.

    ``None`` mirrors what the previous ``git hash-object`` call site did on a
    non-zero exit (untracked file, unreadable path): the caller leaves the hash
    unset and falls back to its own content hash.
    """
    try:
        with open(path, "rb") as handle:
            # fstat the OPEN descriptor, not the path: the length baked into the
            # header and the bytes hashed after it must describe the same inode,
            # or a file replaced between the two steps yields a hash of nothing
            # real. Streaming (rather than reading the file whole) keeps a large
            # file out of memory, so the size has to be known up front.
            size = os.fstat(handle.fileno()).st_size
            digest = _blob_digest(size)
            read = 0
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                digest.update(chunk)
            if read != size:
                # Truncated or appended to while being read. Refuse rather than
                # return a hash that matches no actual content; the caller falls
                # back to its own file hash exactly as it does for an OSError.
                return None
        return digest.hexdigest()
    except OSError:
        return None


def git_blob_hashes(
    paths: Iterable[Union[str, Path]], *, root: Optional[Path] = None
) -> Dict[str, str]:
    """Blob-hash many paths, keyed by the path as given.

    Replaces ``git hash-object --stdin-paths``. Entries that cannot be read are
    omitted rather than raising, so a single unreadable file does not discard the
    whole batch the way a failed subprocess did.
    """
    out: Dict[str, str] = {}
    for item in paths:
        key = str(item)
        target = (root / key) if root is not None else Path(key)
        digest = git_blob_hash(target)
        if digest is not None:
            out[key] = digest
    return out
