"""Utilities for CoW/NFS-resilient filesystem operations.

Bug #1229: os.makedirs(path, exist_ok=True) raises FileExistsError when
`path` is a DANGLING SYMLINK (the link entry exists but its target is gone).
exist_ok only suppresses the error when path resolves to an existing directory.

On a cluster node where golden-repos / activated-repos are symlinks into a
CoW/NFS mount, a mount going down makes those symlinks dangle.  Calling
makedirs on them then crashes every uvicorn worker in a tight CRASH LOOP,
turning a single-node storage failure into a full app-layer outage.

The _safe_makedirs_cow helper detects this case and logs a single degraded-mode
WARNING instead of raising, so workers can start up and serve non-CoW traffic
(auth, health, node metrics) while the mount is unavailable.

Key invariants:
- NEVER delete or replace the symlink (Bug #1052 -- the symlink must re-bind
  when the mount returns).
- Only the DANGLING case (islink=True AND target does not resolve to a dir) is
  treated as degraded; all other cases behave byte-identically to makedirs.
- Uses os.path.islink (lstat, non-traversing) for the existence check first,
  then os.path.isdir (follows the link) only when we know it is a symlink.
  The hang variant (hard-NFS target that stalls on stat) is out of scope --
  that is an infra soft-mount concern; this fix addresses the fast Errno 17
  crash-loop case.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Log the degraded warning at most once per distinct path per process so that
# repeated calls (e.g. from a tight health-check loop during an outage) do not
# flood the log.
_warned_paths: set = set()


def _safe_makedirs_cow(path: str) -> None:
    """Like os.makedirs(path, exist_ok=True) but resilient to dangling symlinks.

    When `path` is a dangling symlink (link entry exists, target directory
    does NOT exist -- i.e. the CoW/NFS mount is unavailable), this function:
      - does NOT call os.makedirs (which would raise FileExistsError Errno 17)
      - does NOT delete or replace the symlink
      - logs ONE rate-limited WARNING and returns so startup can continue

    All other cases are delegated to os.makedirs(path, exist_ok=True) and
    behave identically to the pre-fix code:
      - normal missing directory  -> create it (and any parents)
      - existing real directory   -> no-op
      - valid symlink -> real dir -> no-op
    """
    if os.path.islink(path) and not os.path.isdir(path):
        # Dangling symlink: the CoW/NFS mount is unavailable.
        # Do NOT makedirs (crashes with FileExistsError/Errno 17).
        # Do NOT remove/replace the symlink (Bug #1052).
        if path not in _warned_paths:
            _warned_paths.add(path)
            logger.warning(
                "CoW storage unavailable at %s (dangling symlink) -- "
                "starting in degraded mode; golden/activated repos inaccessible "
                "until the mount returns",
                path,
            )
        return

    # Normal path: behaves exactly like os.makedirs(path, exist_ok=True).
    os.makedirs(path, exist_ok=True)
