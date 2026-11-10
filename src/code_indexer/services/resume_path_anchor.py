"""Re-anchoring of file paths a PRIOR indexing run persisted (its recorded
failures) onto the CURRENT walk root. Used by SmartIndexer's failure retry
and by the server's reader of recorded failures (Bug #2125), so both treat
a stored path exactly alike."""

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from ..indexing.repo_manifest import RepoManifest


def reanchor_resume_path(
    stored_path: str,
    codebase_dir: Path,
    manifest: Optional["RepoManifest"] = None,
) -> Path:
    """Re-anchor a stored resume path onto the CURRENT walk root.

    The failure retry reads file paths persisted by a PRIOR run
    (``ProgressiveMetadata.set_failed_file_paths`` stores ``str(path)``).
    Those strings are absolute paths carrying whatever filesystem prefix
    the prior run accessed the repo under. On the cow-daemon backend the
    same logical directory is reachable under TWO different absolute
    prefixes -- the NFS *mount* path (e.g.
    ``/mnt/cow-storage/golden-repos/<repo>``) and the daemon-LOCAL path
    (e.g. ``/home/opuser/cow-storage/golden-repos/<repo>``). If the
    prior run stored the mount prefix but the current run's
    ``codebase_dir`` is the daemon-local prefix (or vice versa), using the
    stored path verbatim makes ``file_path.relative_to(codebase_dir)``
    raise ``ValueError: ... is not in the subpath of ...`` -> the staging
    "Hash calculation failed" / "Git-aware resume failed" loop.

    Re-anchoring rules (path-domain agnostic, walk-root authoritative):
    - A RELATIVE stored path joins onto ``codebase_dir`` (legacy behavior).
    - An ABSOLUTE stored path already under ``codebase_dir`` is returned
      unchanged -- the normal / local-backend case stays byte-identical.
    - An ABSOLUTE stored path with a stale prefix is re-anchored by
      locating the ``codebase_dir`` leaf name within the stored path's
      parts and joining the trailing subpath onto ``codebase_dir``.
    - If no leaf match is found the stored path is returned UNCHANGED so
      the downstream ``.exists()`` filter (or a genuine error) still
      surfaces -- we never fabricate a wrong mapping (anti-silent-failure).

    Args:
        stored_path: Path string as persisted by a prior run.
        codebase_dir: The current walk root (``config.codebase_dir``).
        manifest: the pass's delegated manifest, which answers "exists" for a
            repository not on this machine; None asks the disk.

    Returns:
        A Path re-anchored under ``codebase_dir`` when possible.
    """
    return _reanchor(
        stored_path,
        codebase_dir,
        (lambda path: path in manifest)
        if manifest is not None
        else (lambda path: path.exists()),
    )


def reanchor_resume_path_lexically(stored_path: str, codebase_dir: Path) -> Path:
    """:func:`reanchor_resume_path` with no existence answers at all, for a
    caller whose repository is not on this machine and that holds no pass
    manifest (the refresh counting what a delegated child recorded as
    failed): a stale prefix maps to its FIRST leaf match, the documented
    fallback, and nothing on this disk is consulted."""
    return _reanchor(stored_path, codebase_dir, lambda path: False)


def _reanchor(
    stored_path: str, codebase_dir: Path, present: Callable[[Path], bool]
) -> Path:
    candidate = Path(stored_path)

    # Relative stored path -> join onto the current walk root (legacy).
    if not candidate.is_absolute():
        return codebase_dir / candidate

    # Already under the current walk root -> no change (normal/local case).
    try:
        candidate.relative_to(codebase_dir)
        return candidate
    except ValueError:
        pass

    # Stale-prefix case: re-anchor by matching the codebase leaf name.
    # Scan FORWARD so that when the leaf appears more than once (bug #1087,
    # e.g. golden-repos/fastapi/tests/fastapi/conftest.py) we try each
    # candidate in order and return the first one that exists on disk.
    # When no candidate exists (e.g. synthetic paths in tests, or the file
    # was genuinely deleted) we fall back to the FIRST match found, which
    # preserves the single-occurrence behavior that callers relied on before.
    leaf = codebase_dir.name
    parts = candidate.parts
    first_match: Optional[Path] = None
    for index in range(len(parts)):
        if parts[index] == leaf:
            tail = parts[index + 1 :]
            anchored = codebase_dir.joinpath(*tail) if tail else codebase_dir
            if present(anchored):
                return anchored
            if first_match is None:
                first_match = anchored

    # Return the first (shallowest) match if any were found; otherwise
    # return unchanged so downstream .exists()/.relative_to can surface it.
    return first_match if first_match is not None else candidate
