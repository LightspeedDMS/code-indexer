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
        candidate.relative_to(repo_root_resolved)
    except ValueError:
        raise PermissionError("Access denied")
    return candidate


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
    if ".git" in rel.parts:
        raise PermissionError(
            f"{operation} blocked: Access to .git/ directory is forbidden"
        )
