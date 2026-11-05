"""A file's size and mtime: from the pass's manifest when there is one, else a stat.

Under a delegated content backend the repository is not on this container's disk,
so a ``stat`` either fails or -- worse, while a mount is still present -- answers
about a copy the index is not being built from. The manifest already holds both
values for every file, taken in the same pass as the blob hashes.

There is deliberately NO fallback to ``stat`` when a manifest is present. A
manifest miss means the file was not there when the pass began; it reports as
unknown (size 0, mtime None) rather than reaching for a disk that may not be the
repository at all. Without a manifest -- every local pass -- this is exactly the
``stat`` the call sites made before, including the exceptions it raises.

Kept free of server imports: the manifest type is for type-checking only, so
plain CLI indexing never loads the content seam.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from .repo_manifest import RepoManifest


class ContentUnavailableError(RuntimeError):
    """A content source could not supply a file's bytes (not merely: it is gone)."""


def file_size(file_path: Path, manifest: Optional["RepoManifest"]) -> int:
    """Bytes in the file. Raises like ``Path.stat`` when there is no manifest."""
    if manifest is None:
        return Path(file_path).stat().st_size
    return manifest.size(file_path) or 0


def file_mtime_and_size(
    file_path: Path, manifest: Optional["RepoManifest"]
) -> Tuple[Optional[float], int]:
    """``(st_mtime, st_size)``. Raises like ``Path.stat`` when there is no manifest."""
    if manifest is None:
        stat = Path(file_path).stat()
        return stat.st_mtime, stat.st_size
    return manifest.mtime(file_path), manifest.size(file_path) or 0
