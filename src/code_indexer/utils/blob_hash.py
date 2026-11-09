"""Compute git blob hashes without invoking git.

A git blob hash is ``sha1(b"blob <bytesize>\\0" + content)``. Nothing about that
needs the git binary, an object database, or a subprocess -- it is a pure
function of the bytes on disk.

Why this exists rather than ``git hash-object``:

* **Cost.** The blob hash is computed PER FILE on the indexing path
  (``FileIdentifier._get_git_metadata``), so shelling out means one fork+exec per
  file. At fleet scale (~900 repos) that is the single highest-volume git
  invocation cidx makes.
* **Seam discipline.** ``git_backend`` delegates repo-level git work to the git service.
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

The functions are the content seam's definition of content identity, so they
live in the SDK (``cidx_sdk.seams.content_identity``) where every content
backend -- local or delegated -- can import the ONE formula; this module
re-exports them.
"""

from __future__ import annotations

from cidx_sdk.seams.content_identity import (
    git_blob_hash as git_blob_hash,
    git_blob_hash_bytes as git_blob_hash_bytes,
    git_blob_hashes as git_blob_hashes,
)

__all__ = ["git_blob_hash", "git_blob_hash_bytes", "git_blob_hashes"]
