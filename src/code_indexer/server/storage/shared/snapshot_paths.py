"""Canonical versioned-snapshot path convention (Bug #1084 Phase A1).

This module is the **single source of truth** for deciding whether an absolute
filesystem path is a versioned snapshot of a golden repository. Before Bug #1084
this decision was made by a brittle ``".versioned" in path`` substring test
duplicated across three cleanup gates plus a dozen secondary consumers — a test
that only happened to work on the LocalCloneBackend layout and silently leaked
every snapshot on the cow-daemon and ONTAP backends.

Canonical layout (every backend, going forward)::

    <snapshot_root>/.versioned/{namespace}/v_<unix_ts>

where ``snapshot_root`` is ``golden_repos_dir`` (local) or ``mount_point``
(cow-daemon / ONTAP). The predicate recognizes this shape with NO backend
knowledge required.

Transition clause (recognition only — NEVER created going forward): legacy
cow-daemon snapshots live at ``{mount}/{namespace}/v_<unix_ts>`` and ONTAP
snapshots at ``{mount}/v_<unix_ts>`` — neither has a ``.versioned`` segment.
These are recognized ONLY when the caller supplies ``mount_point`` (so the
manager, which knows the backend mount, can clean up superseded legacy
snapshots while they rotate out). Without ``mount_point`` the predicate is
conservative and returns ``False`` for legacy shapes, because a bare
``{parent}/{name}`` path is indistinguishable from a non-snapshot.

The ``{mount}/activated-repos/...`` subtree (Bug #1052 activation clones living
under the same mount) MUST test ``False``; the master base clone
(``golden_repos_dir/{repo}``) MUST test ``False``.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Optional

#: A snapshot leaf is exactly ``v_`` followed by one or more digits.
_V_TIMESTAMP_RE = re.compile(r"^v_\d+$")

#: Canonical marker segment present in every going-forward snapshot path.
_VERSIONED_SEGMENT = ".versioned"

#: Subtree under the cow-daemon mount that holds activation clones (Bug #1052),
#: which must never be mistaken for a versioned snapshot.
_ACTIVATED_REPOS_SEGMENT = "activated-repos"


def _is_v_timestamp(component: str) -> bool:
    """Return True if *component* is a ``v_<digits>`` snapshot leaf name."""
    return bool(_V_TIMESTAMP_RE.match(component))


def is_versioned_snapshot(path: str, *, mount_point: Optional[str] = None) -> bool:
    """Return ``True`` when *path* is a versioned snapshot of a golden repo.

    Parameters
    ----------
    path:
        Absolute (or relative) filesystem path to test. ``None``, empty, and
        malformed inputs return ``False`` rather than raising.
    mount_point:
        Optional backend mount point. When supplied, enables recognition of the
        legacy cow-daemon shape ``{mount}/{ns}/v_<ts>`` and the flat ONTAP shape
        ``{mount}/v_<ts>`` (transition clause). When ``None``, only the canonical
        ``.versioned`` shape is recognized.

    Returns
    -------
    bool
        ``True`` iff *path* matches the canonical snapshot shape, or (when
        *mount_point* is given) the legacy/ONTAP transition shape.
    """
    if not path:
        return False

    # Normalise: drop trailing slash(es) and split into POSIX path components.
    pure = PurePosixPath(path)
    parts = pure.parts
    if not parts:
        return False

    # --- Canonical clause: .../.versioned/{ns}/v_<ts> -------------------------
    # The leaf must be v_<ts>, its immediate parent is the namespace dir, and the
    # grandparent must be the literal ``.versioned`` segment.
    if (
        len(parts) >= 3
        and _is_v_timestamp(parts[-1])
        and parts[-3] == _VERSIONED_SEGMENT
    ):
        # Guard: the namespace component itself must not be ``.versioned`` and
        # must not be the activated-repos subtree.
        namespace = parts[-2]
        if namespace not in (_VERSIONED_SEGMENT, _ACTIVATED_REPOS_SEGMENT):
            return True

    # --- Transition clause: legacy cow-daemon / flat ONTAP shapes -------------
    # Only attempted when the caller supplies the backend mount point.
    if mount_point:
        mount = PurePosixPath(mount_point.rstrip("/"))
        try:
            relative = pure.relative_to(mount)
        except ValueError:
            # Path is not under this mount — canonical clause already had its say.
            return False

        rel_parts = relative.parts

        # Flat ONTAP shape: {mount}/v_<ts>  (single component)
        if len(rel_parts) == 1 and _is_v_timestamp(rel_parts[0]):
            return True

        # Legacy cow-daemon shape: {mount}/{ns}/v_<ts>  (exactly two components)
        if (
            len(rel_parts) == 2
            and _is_v_timestamp(rel_parts[1])
            and rel_parts[0] != _ACTIVATED_REPOS_SEGMENT
        ):
            return True

    return False
