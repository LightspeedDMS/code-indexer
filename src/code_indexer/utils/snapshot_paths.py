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
from typing import Callable, Optional

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


def _default_predicate(mount_point: Optional[str]) -> Callable[[str], bool]:
    def check(candidate: str) -> bool:
        return is_versioned_snapshot(candidate, mount_point=mount_point)

    return check


def resolve_versioned_snapshot_root(
    path: str,
    *,
    mount_point: Optional[str] = None,
    predicate: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """Bug #1850: bridge an index-dir cache key to its snapshot root.

    Reader-lease caches key their guard by the index directory, a
    descendant of the real snapshot root that ``is_versioned_snapshot()``
    recognizes. This walks ancestors of *path*, delegating each check to
    the canonical predicate (never reimplementing its matching logic), and
    returns the nearest matching ancestor or ``None``. ``predicate`` lets
    CLI-reachable callers pass an already-injected ``is_versioned_snapshot``
    callable instead of importing this (server-only) module directly.
    """
    if not path:
        return None
    check = predicate if predicate is not None else _default_predicate(mount_point)
    pure = PurePosixPath(path)
    for candidate in (pure, *pure.parents):
        if check(str(candidate)):
            return str(candidate)
    return None


def is_immutable_versioned_snapshot(path: str) -> bool:
    """Return True ONLY for a path at or inside an immutable canonical snapshot.

    Bug #1084 B4: the ``.versioned`` decision is delegated to the SINGLE canonical
    predicate (``snapshot_paths.is_versioned_snapshot``), CANONICAL clause only
    (``mount_point`` omitted). The immutable golden-repo snapshot layout (project
    CLAUDE.md "Golden Repo Versioned Path") is ``.../.versioned/{alias}/v_{ts}/``;
    a refresh produces a NEW ``v_*`` directory (a new key / cache miss), never an
    in-place mutation, so its config can be cached with NO TTL. Canonical
    cow-daemon snapshots (``{mount}/.versioned/{ns}/v_*``) now legitimately gain
    NO-TTL caching.

    LEGACY cow shapes (``{mount}/{ns}/v_*``) and flat ONTAP (``{mount}/v_*``) are
    deliberately NOT recognized here (the mount point is intentionally withheld):
    those predate retention and could be deleted, so the NO-TTL immutability
    promise must not extend to them -- they stay on the SHORT-TTL cache, the safe
    (self-healing) direction. The MUTABLE base clone and activated CoW clones are
    likewise rejected and MUST use the default SHORT-TTL cache.

    The canonical predicate matches the snapshot LEAF (``v_<ts>``); a query path
    is frequently DEEPER (e.g. ``.../v_42/.code-indexer/config.json``) and is
    equally immutable. So we also test the path's ancestors: True iff the path
    itself OR any ancestor is a canonical snapshot. Purely structural (no
    filesystem access, NFS-safe); traversal tokens are rejected by the canonical
    predicate's normalization.
    """
    if not path:
        return False

    if "/../" in path or path.endswith("/..") or path.startswith("../") or path == "..":
        # Reject traversal tokens up front (Story #1082 contract). The canonical
        # predicate would also reject the resulting shape, but be explicit.
        return False

    if is_versioned_snapshot(path):
        return True

    # Walk ancestors: a subpath inside a canonical snapshot is still immutable.
    pure = PurePosixPath(path)
    for ancestor in pure.parents:
        ancestor_str = str(ancestor)
        if ancestor_str in ("", "/", "."):
            break
        if is_versioned_snapshot(ancestor_str):
            return True

    return False
