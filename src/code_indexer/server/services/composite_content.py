"""A composite activated repository's member files, when they are not on this pod.

A composite repository is ``<activated_repos_dir>/<user>/<alias>/`` holding one
working copy per member, ``<alias>/<member>``. Locally the members are
directories on this disk and the composite listing and details walk them. Under
``content_backend='delegated'`` (docs/content-seam-activated-repos.md) every
member is a working copy on workspace-mcp's storage at the same absolute path,
so its files are read through the content seam's manifest, and the member list
comes from the registry (the activation's ``discovered_repos``) -- never from a
walk of this pod's directory, which holds index state only.

Fail-loud rule: a backend failure -- a member missing on workspace-mcp included
-- raises. It is never read as "no files".
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Union

from code_indexer.server.services.file_service import (
    ContentSeamSource,
    content_seam_source,
    walk_manifest_entries,
)
from code_indexer.server.storage.shared.content_backend import ContentEntry
from code_indexer.utils.index_store import INDEX_DIRNAME

logger = logging.getLogger(__name__)

_CONSUMER = "composite repository"

#: Directory names a composite listing never shows (local: skipped while
#: walking; delegated: pruned by the manifest, ``.git`` always is).
SKIPPED_DIRS: Tuple[str, ...] = (".git", INDEX_DIRNAME)


class CompositeMemberUnreadableError(RuntimeError):
    """A member's files could not be described by the content backend."""


def composite_seam_source(
    composite_path: Union[str, Path],
) -> Optional[ContentSeamSource]:
    """The seam a composite's members are read through, or None (local disk)."""
    return content_seam_source(str(composite_path), consumer=_CONSUMER)


def plain_member_names(members: Iterable[object], composite_path: object) -> List[str]:
    """*members* that are one plain path component, in order.

    A member name joins onto the composite's path; anything else (``..``, a
    separator, empty) would reach outside the composite, so it is skipped and
    logged rather than used.
    """
    safe: List[str] = []
    for member in members:
        name = str(member)
        if name in ("", ".", "..") or "/" in name or "\\" in name:
            logger.warning(
                "Skipping composite member %r of %s: not a plain name",
                name,
                composite_path,
            )
            continue
        safe.append(name)
    return safe


def member_entries(
    source: ContentSeamSource, member_path: Union[str, Path]
) -> Tuple[ContentEntry, ...]:
    """Every file of one member working copy (``.git`` and index dirs pruned).

    Never cached: the copy is writable. Raises
    :class:`CompositeMemberUnreadableError` on any backend failure.
    """
    member_source = ContentSeamSource(backend=source.backend, root=str(member_path))
    try:
        return walk_manifest_entries(member_source, SKIPPED_DIRS)
    except Exception as e:
        raise CompositeMemberUnreadableError(
            f"Cannot list composite member '{member_path}' through the content "
            f"backend: {type(e).__name__}: {e}"
        ) from e


def modified_from_ns(mtime_ns: Optional[int], path: str) -> datetime:
    """A manifest ``mtime_ns`` as the naive local ``datetime`` a local
    ``datetime.fromtimestamp(stat().st_mtime)`` gives.

    An unknown time raises: inventing one (the epoch, "now") would publish a
    wrong date as fact, and skipping the file would shrink the listing.
    """
    if mtime_ns is None:
        raise CompositeMemberUnreadableError(
            f"The content backend gave no modification time for '{path}' "
            "(a workspace-mcp without manifest mtimes?)"
        )
    seconds, nanos = divmod(mtime_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds + nanos * 1e-9)
