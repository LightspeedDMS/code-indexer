"""Path-confinement primitive shared by every server-side file front door.

Bug #1891 (round 3, D2): originally a staticmethod on
``FileListingService`` (server/services/file_service.py), which pulled
~106 ``code_indexer.server.*`` modules (config_service, auto_update, etc.)
into any caller that merely wanted this stdlib-only helper --
notably ``global_repos/directory_explorer.py``, whose only reason to
import the server layer at all was this one function. Moved here
(stdlib-only: ``os``/``pathlib`` only) so CLI-path and low-layer modules
can use it without paying the server import cost.

No re-export shim on ``FileListingService`` -- every caller imports this
module directly.
"""

from pathlib import Path
from typing import Iterable, Optional

# The repository's git directory. Its contents (configuration with remote
# URLs, hooks, objects) are never served or written through a file front
# door: a repository-relative path with a ``.git`` segment is refused.
GIT_DIRECTORY_NAME = ".git"


def has_git_segment(parts: Iterable[str]) -> bool:
    """True when any path segment is ``.git``, compared case-insensitively
    (a case-insensitive filesystem would resolve ``.GIT`` to the same
    directory). ``.gitignore``, ``.github`` and the like are not ``.git``."""
    return any(part.casefold() == GIT_DIRECTORY_NAME for part in parts)


class GitDirectoryPathError(PermissionError):
    """A confined path lies inside, or is, the repository's ``.git``.

    A PermissionError, so every caller that refuses an out-of-repository
    path refuses this one too; read front doors answer it exactly as they
    answer a nonexistent path."""


# Linux NAME_MAX is 255 bytes. resolve_confined_path rejects any single
# path component longer than this BEFORE it ever reaches a stat/lstat
# syscall -- Path.resolve() does not raise for an overlong component
# (verified empirically: it silently defers the error), so without this
# proactive guard the caller's very next exists()/is_file()/open() raises
# an uncaught OSError (ENAMETOOLONG) instead of the PermissionError every
# caller already handles.
MAX_CONFINED_PATH_COMPONENT_BYTES: int = 255


def resolve_confined_path(repo_root: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` against ``repo_root`` and confine it.

    Bug #1891: the single, reusable path-confinement primitive for
    server-side file-reading front doors. Resolves the candidate path
    (following symlinks) and verifies the result lies inside
    ``repo_root`` using ``Path.relative_to()`` -- immune to the
    sibling-directory prefix-string bug that a bare
    ``str(x).startswith(str(root))`` check is exposed to (e.g.
    repo_root=/data/repo would incorrectly admit /data/repo-evil under
    a naive string check; relative_to() never does).

    MUST be called BEFORE any exists()/is_file()/stat()/open() on the
    caller-supplied path -- Path.resolve() never raises for a
    nonexistent target, so this confinement check is the only gate
    standing between a caller-supplied path and the filesystem.

    Args:
        repo_root: Repository root directory (need not be pre-resolved).
        relative_path: User-supplied path, taken as relative to
            repo_root. May contain parent-traversal ("../x") segments,
            be an absolute path (which escapes repo_root entirely once
            joined, per pathlib semantics), or pass through a symlink
            that points outside the repository -- all three are
            resolved and rejected here.

    Returns:
        The resolved, confined absolute Path.

    Raises:
        PermissionError: If the resolved target lies outside
            repo_root, if relative_path is not a string (e.g. ``None``
            -- callers deserializing untrusted request bodies can hand
            this a non-str value; without this guard the very next
            ``Path.__truediv__`` raises an uncaught ``TypeError``), or
            if relative_path is malformed in a way that would
            otherwise raise a raw filesystem/encoding exception (Bug
            #1891 round 2, S4; round 3 P3): an embedded NUL byte or a
            symlink loop make Path.resolve() raise ValueError/
            RuntimeError (platform/version-dependent, OSError on
            some), a lone UTF-16 surrogate code point (e.g. "\\ud800",
            never valid in a real filesystem path) makes the
            proactive component-length encode() raise
            UnicodeEncodeError (a ValueError subclass), and an
            overlong path component would make a caller's very next
            exists()/is_file()/open() raise OSError (ENAMETOOLONG) --
            all are normalized to PermissionError here so every
            caller's existing PermissionError handling covers them
            too, instead of an uncaught exception reaching a REST/MCP/
            CRUD front door as a 500 or unhandled traceback.
    """
    if not isinstance(relative_path, str):
        raise PermissionError("Access denied")

    # Proactively reject an overlong component BEFORE any syscall.
    # Path.resolve() itself does not raise for this input -- it is the
    # caller's next filesystem call that would raise OSError, and by
    # then it is too late to convert cleanly.
    #
    # Also catches UnicodeError here (round 3 P3): a lone surrogate code
    # point (e.g. "\ud800") is not representable in UTF-8 and makes
    # .encode("utf-8", "surrogateescape") raise UnicodeEncodeError --
    # "surrogateescape" only round-trips the specific U+DC80-U+DCFF range
    # produced by *decoding* with that handler, not an arbitrary lone
    # surrogate appearing in a caller-supplied string.
    try:
        for _component in relative_path.split("/"):
            if (
                len(_component.encode("utf-8", "surrogateescape"))
                > MAX_CONFINED_PATH_COMPONENT_BYTES
            ):
                raise PermissionError("Access denied")
    except UnicodeError as exc:
        raise PermissionError("Access denied") from exc

    try:
        repo_root_resolved = Path(repo_root).resolve()
        candidate = (Path(repo_root) / relative_path).resolve()
    except (ValueError, RuntimeError, OSError) as exc:
        raise PermissionError("Access denied") from exc

    try:
        relative = candidate.relative_to(repo_root_resolved)
    except ValueError:
        raise PermissionError("Access denied")
    # Checked on the RESOLVED location, so "./.git", "a/../.git" and a
    # symlink into .git are all refused.
    if has_git_segment(relative.parts):
        raise GitDirectoryPathError("Access denied")
    return candidate


def resolve_if_within_root(candidate: Path, resolved_root: Path) -> Optional[Path]:
    """Resolve ``candidate`` (following symlinks, collapsing ``..``
    segments) and return the resolved path if it lies inside
    ``resolved_root``, else ``None``.

    The core implementation of the shared containment primitive used by
    every indexing discovery and file-read path -- a fresh directory
    walk, git-diff-based incremental discovery, watch-mode handlers, the
    disk-vs-database reconcile pass, the interrupted-operation resume
    path, and the point where a file's content is actually opened for
    chunking. ``is_resolved_within_root``
    is a thin boolean wrapper around this function -- callers that also
    need the resolved value itself (e.g. to derive a relative path) call
    this one directly instead of resolving the same candidate a second
    time.

    ``resolved_root`` MUST already be resolved (``Path.resolve()``) by
    the caller -- this function resolves it ONCE per run, not once per
    candidate, so callers checking many candidates against the same root
    (a full directory walk, a batch of git-diff entries) should resolve
    the root a single time and pass the same value for every candidate.

    Fails closed (returns ``None``, never raises) on any resolution
    error, including a symlink loop (``RuntimeError`` on CPython) or a
    permission error walking the link chain (``OSError``), and on a
    resolved location that is not a descendant of ``resolved_root``.

    Args:
        candidate: The path to check (need not exist; need not be
            pre-resolved).
        resolved_root: The already-resolved root directory candidates
            must lie inside.

    Returns:
        The resolved candidate path if it lies inside resolved_root,
        else None.
    """
    try:
        resolved_candidate = candidate.resolve()
    except (OSError, RuntimeError):
        return None

    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        return None

    return resolved_candidate


def is_resolved_within_root(candidate: Path, resolved_root: Path) -> bool:
    """Return True if ``candidate`` resolves (following symlinks,
    collapsing ``..`` segments) to a location inside ``resolved_root``.

    Thin boolean wrapper around ``resolve_if_within_root`` -- see that
    function's docstring for the full contract (fail-closed behavior,
    the ``resolved_root`` pre-resolution requirement, and the list of
    callers).
    """
    return resolve_if_within_root(candidate, resolved_root) is not None


def is_readable_within_root(candidate: Path, resolved_root: Path) -> bool:
    """The read-side rule for a repository file: True only when ``candidate``
    resolves (following symlinks, collapsing ``..``) inside
    ``resolved_root`` AND its RESOLVED location, relative to the root, has
    no ``.git`` segment. A committed symlink such as ``link.py ->
    .git/config`` or ``gitdir -> .git`` is therefore refused whatever its
    own name. ``resolved_root`` must already be resolved. Fails closed on
    any resolution error."""
    resolved = resolve_if_within_root(candidate, resolved_root)
    if resolved is None:
        return False
    return not has_git_segment(resolved.relative_to(resolved_root).parts)


def resolves_into_git_directory(candidate: Path, resolved_root: Path) -> bool:
    """True when ``candidate`` resolves inside ``resolved_root`` AND its
    resolved location, relative to the root, has a ``.git`` segment (e.g. a
    committed ``link.py -> .git/config``). A target outside the root
    returns False, so callers keep their own outside-root behaviour while a
    file resolving into the repository's own .git is excluded in every
    mode. ``resolved_root`` must already be resolved."""
    resolved = resolve_if_within_root(candidate, resolved_root)
    return resolved is not None and has_git_segment(
        resolved.relative_to(resolved_root).parts
    )


def is_indexable_location(candidate: Path, resolved_root: Path, confined: bool) -> bool:
    """The indexing-side location rule for a file about to be read.

    Server context (``confined``): ``is_readable_within_root`` -- inside the
    root and outside its ``.git``. Local CLI context: any location (a
    symlink may point outside the repository) except one resolving into
    the repository's own ``.git``. ``resolved_root`` must already be
    resolved."""
    if confined:
        return is_readable_within_root(candidate, resolved_root)
    return not resolves_into_git_directory(candidate, resolved_root)


def reject_if_within_git_directory(
    repo_root: Path, resolved_path: Path, operation: str
) -> None:
    """Reject a CONFINED path if it lies inside (or IS) the repo's ``.git``.

    Bug #1891 (final round, SECURITY): ``resolve_confined_path()`` only
    confines a caller-supplied path to ``repo_root`` -- it has no
    knowledge of git semantics. A symlink tracked INSIDE the repository
    (e.g. ``gitlink -> .git``, which survives ``git clone`` when
    committed) is itself confined to ``repo_root``, so a request like
    ``"gitlink/hooks/pre-commit"`` resolves to
    ``repo_root/.git/hooks/pre-commit`` -- a path that passes
    ``resolve_confined_path()``'s containment check yet still escapes
    into git internals. A planted hook there executes on the server's
    next ``git commit`` (run without ``--no-verify``).

    Call this AFTER ``resolve_confined_path()``, passing its return
    value (or an equally-confined derivative, e.g. a confined parent
    directory joined with a literal final-component name). Rejects
    whenever ``.git`` appears anywhere in ``resolved_path``'s
    components relative to ``repo_root`` -- covers ``.git/`` as a
    directory, ``.git`` as a plain file (git worktree/submodule
    gitfile), and ``resolved_path`` BEING ``.git`` itself.

    Args:
        repo_root: Repository root (same value passed to
            resolve_confined_path -- need not be pre-resolved).
        resolved_path: A path already confined to repo_root.
        operation: Operation name for the error message -- matches the
            wording of the existing literal ".git" component check in
            FileCRUDService._validate_crud_path.

    Raises:
        PermissionError: If resolved_path lies inside, or is, ``.git``.
    """
    repo_root_resolved = Path(repo_root).resolve()
    try:
        rel = resolved_path.relative_to(repo_root_resolved)
    except ValueError:
        # Not confined to repo_root -- resolve_confined_path() should
        # already have rejected this; nothing further to do here.
        return
    if has_git_segment(rel.parts):
        raise GitDirectoryPathError(
            f"{operation} blocked: Access to .git/ directory is forbidden"
        )
