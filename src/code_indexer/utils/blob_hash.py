"""Compute git blob hashes without invoking git.

A git blob hash is ``sha1(b"blob <bytesize>\\0" + content)``. Nothing about that
needs the git binary, an object database, or a subprocess -- it is a pure
function of the bytes on disk.

Why this exists rather than ``git hash-object``:

* **Cost.** The blob hash is computed PER FILE on the indexing path
  (``FileIdentifier._get_git_metadata``), so shelling out means one fork+exec per
  file. At fleet scale (~900 repos) that is the single highest-volume git
  invocation cidx makes.
* **Seam discipline.** ``git_backend`` delegates repo-level git work to git-mcp.
  A per-file operation is the wrong shape for that seam -- delegating it would
  turn one fork per file into one HTTP round-trip per file. The right answer for
  this particular operation is not to delegate it but to stop needing git at all.

EQUIVALENCE AND ITS ONE LIMIT. This reproduces ``git hash-object --no-filters``
exactly. Plain ``git hash-object`` additionally applies any content-rewriting
``.gitattributes`` -- ``text``/``text=auto``/``eol=`` (CRLF normalisation) and
``filter=`` (e.g. LFS clean) -- and would differ for a file those actually
rewrite. That matters here because the blob hash seeds the deterministic point
UUID for every indexed chunk (``GitAwareDocumentProcessor``), so a changed hash
re-identifies chunks rather than merely invalidating them.

Measured on the live fleet before adopting this: of 923 golden repos, 20 declare
content-rewriting attributes; across the 6 highest-risk of those (both
``filter=lfs`` repos included) 3,473 tracked files hashed IDENTICALLY under git
and under this function. The reason is mechanical, not luck -- the clones are
checked out on Linux so ``text``/``eol=lf`` normalisation is already a no-op, and
the only ``filter=lfs`` pattern is ``*.pth``, which no checkout carries and which
could not run anyway because git-lfs is not installed in the image.

If git-lfs is ever added, or a working tree ever holds CRLF content under
``text=auto``, re-measure before trusting this for those repos.
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
