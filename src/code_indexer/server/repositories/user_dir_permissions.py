"""Permission contract for the per-user ``activated-repos/<user>/`` directory.

Two different OS users must be able to create entries in that directory:

* the cidx-server service user -- it creates the directory and writes
  ``<alias>_metadata.json`` into it;
* in cluster mode, the CoW storage daemon's own user -- it materialises the
  clone ``<user>/<alias>`` there (and removes it on deactivation).

The daemon user gets access through membership of the service group (the
``service_group`` in the daemon's own config), provisioned by
``scripts/install-cidx-server.sh`` and self-healed by the auto-updater
(``DeploymentExecutor._ensure_cow_daemon_user_in_service_group``). That
membership only helps if the directory is group-writable, so the mode is
pinned explicitly here instead of being left to the process umask (the
systemd default 022 yields 0755, which locks the daemon out).

0o2775: owner rwx, group rwx plus setgid (entries created inside, including
the daemon's clone directory, inherit the directory's group), others r-x.
Never world-writable.
"""

import os
import stat

USER_DIR_MODE = 0o2775

# Bits that must be present for the owner and the service group to create
# entries, plus setgid for group inheritance.
_REQUIRED_BITS = stat.S_ISGID | stat.S_IRWXU | stat.S_IRWXG


def ensure_activated_user_dir(user_dir: str) -> None:
    """Create ``user_dir`` with USER_DIR_MODE, or repair it when owned by us.

    * Missing: created (parents included), its group set to this process's
      primary gid (the service group the CoW daemon is joined to -- a setgid
      parent would otherwise impose ITS group), then chmod-ed to
      USER_DIR_MODE, so the result depends on neither umask nor parent.
    * Existing and owned by this process's effective uid: group normalised to
      the primary gid when it differs, and missing group rwx/setgid bits
      added with all other bits preserved (a directory created by an older
      server under umask 022). Converges already-broken directories on the
      next activation.
    * Existing and owned by another user: left untouched -- it was provisioned
      by someone else and this process could not chown/chmod it anyway.

    Raises:
        NotADirectoryError: ``user_dir`` exists but is not a directory.
        OSError: creation, chown or chmod failed (surfaced, never swallowed).
    """
    try:
        os.makedirs(user_dir)
    except FileExistsError:
        _repair_existing_user_dir(user_dir)
        return
    _ensure_primary_group(user_dir, os.stat(user_dir).st_gid)
    os.chmod(user_dir, USER_DIR_MODE)


def _ensure_primary_group(user_dir: str, current_gid: int) -> None:
    # chown BEFORE chmod: a group change must never be able to drop setgid.
    if current_gid != os.getgid():
        os.chown(user_dir, -1, os.getgid())


def _repair_existing_user_dir(user_dir: str) -> None:
    st = os.stat(user_dir)
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(
            f"activated-repos user path exists but is not a directory: {user_dir}"
        )
    if st.st_uid != os.geteuid():
        return
    _ensure_primary_group(user_dir, st.st_gid)
    current = stat.S_IMODE(os.stat(user_dir).st_mode)
    if current & _REQUIRED_BITS == _REQUIRED_BITS:
        return
    os.chmod(user_dir, current | _REQUIRED_BITS)
