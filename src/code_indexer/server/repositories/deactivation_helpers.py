"""Story #1032 deactivation helpers — fd-anchored filesystem ops.

Module-level pure functions extracted from `activated_repo_manager.py` per opus
review M3 (MESSI Rule 6 — 500-line module cap). Manager file was 3888 lines
before extraction. These four helpers carry zero instance state — they operate
entirely on string paths + file descriptors — so module-level extraction is
correctness-preserving.

Public surface (re-exported from `activated_repo_manager` for backward compat
with tests that patch the manager namespace):
  - _safe_purge_trash_entry(trash_root, entry_name) -> None
  - _fd_anchored_rmtree(name, parent_fd, expected_st_dev) -> None
  - _fd_anchored_phase1_rename(activated_repos_dir, username, user_alias) -> str
  - _predeactivation_leak_scan_enabled() -> bool

Design history: codex GPT-5 reviewed these helpers three times. The current
form (fd-anchored, st_dev-guarded, O_NOFOLLOW, dual rename_was_attempted +
phase1_succeeded flags upstream) is the result of closing TOCTOU + permission-
false-negative attacks the earlier path-based versions had.
"""

import os
import subprocess  # noqa: F401 — preserved for compatibility with subprocess-using paths
import uuid
from datetime import datetime, timezone
from typing import Optional  # noqa: F401 — used in helper signatures


def _safe_purge_trash_entry(trash_root: str, entry_name: str) -> None:
    """Recursively delete `{trash_root}/{entry_name}` via fd-anchored ops only.

    Story #1032 AC7 / AC8.  Codex code review demanded this design: pathname-
    based check-then-delete is racy (TOCTOU via ancestor symlink swap).  This
    function ELIMINATES the attack surface entirely by anchoring every syscall
    to an open directory file descriptor opened with O_DIRECTORY|O_NOFOLLOW.

    Safety invariants enforced BEFORE any unlink:
      1. trash_root is a non-empty string.
      2. entry_name is a non-empty string with NO path separators, NO `..`,
         NO null bytes, NO leading dot-dot-anything.  Must be a simple basename.
      3. trash_root opens cleanly with O_DIRECTORY|O_NOFOLLOW (refuses if root
         is itself a symlink).
      4. entry opens cleanly with O_NOFOLLOW (refuses if entry is a symlink).
      5. Cross-filesystem boundary check: entry must live on same st_dev as
         trash_root.  Refuses to cross filesystem boundaries (different st_dev).
         NOTE: same-superblock bind mounts share st_dev and are NOT detected
         by this check.

    Recursive deletion is performed via os.unlink(name, dir_fd=...) and
    os.rmdir(name, dir_fd=...) -- no path strings ever cross to the kernel
    after the initial fd is opened, so ancestor swaps are impossible.

    Raises:
        ValueError: any safety invariant violated.
        OSError / FileNotFoundError: unlink/rmdir kernel-level failures
            (caller MUST catch and decide; this function does NOT silently
            swallow).
    """
    if not trash_root or not isinstance(trash_root, str):
        raise ValueError(
            f"_safe_purge_trash_entry: refuse empty/non-string trash_root: {trash_root!r}"
        )
    if not entry_name or not isinstance(entry_name, str):
        raise ValueError(
            f"_safe_purge_trash_entry: refuse empty/non-string entry_name: {entry_name!r}"
        )
    if "\x00" in entry_name:
        raise ValueError("_safe_purge_trash_entry: refuse null byte in entry_name")
    if "/" in entry_name or "\\" in entry_name:
        raise ValueError(
            f"_safe_purge_trash_entry: entry_name must be a basename, no path separators: {entry_name!r}"
        )
    if entry_name in (".", "..") or entry_name.startswith(".."):
        raise ValueError(
            f"_safe_purge_trash_entry: refuse '.' / '..' / dot-dot-prefix entry_name: {entry_name!r}"
        )

    # Open trash root with O_DIRECTORY|O_NOFOLLOW -- pins inode; refuses
    # symlinked root.  All subsequent ops are relative to this fd.
    try:
        root_fd = os.open(trash_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except (FileNotFoundError, NotADirectoryError) as e:
        raise ValueError(
            f"_safe_purge_trash_entry: cannot open trash_root {trash_root!r}: {e}"
        ) from e
    except OSError as e:
        # ELOOP raised when root is a symlink under O_NOFOLLOW
        raise ValueError(
            f"_safe_purge_trash_entry: refuse trash_root open failure {trash_root!r}: {e}"
        ) from e

    try:
        root_st = os.fstat(root_fd)
        _fd_anchored_rmtree(entry_name, root_fd, root_st.st_dev)
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass


def _fd_anchored_rmtree(name: str, parent_fd: int, expected_st_dev: int) -> None:
    """Recursively delete `name` (basename) under `parent_fd`.

    All filesystem ops are fd-anchored (dir_fd=parent_fd).  Symlinks are
    unlinked (never followed).  Cross-filesystem boundaries are refused via
    st_dev check at every directory descent -- so an attacker cannot mount
    a different filesystem under our trash mid-purge to redirect the recursion.

    Raises OSError on any unlink/rmdir failure.  Callers handle.
    """
    # Open the entry as a directory with O_NOFOLLOW.  If it's a symlink or
    # not a directory, we fall through to unlink.
    try:
        entry_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except (NotADirectoryError, FileNotFoundError):
        # Symlink or non-directory or missing -- unlink directly (no follow).
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        return
    except OSError:
        # ELOOP for symlink-to-dir under O_NOFOLLOW etc. -- unlink the link.
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        return

    try:
        entry_st = os.fstat(entry_fd)
        if entry_st.st_dev != expected_st_dev:
            raise ValueError(
                f"_fd_anchored_rmtree: refuse cross-filesystem entry {name!r} "
                f"(st_dev={entry_st.st_dev} expected={expected_st_dev})"
            )
        # Enumerate children using the fd (Python supports fd path on Linux).
        # Collect names-only first (cheap strings, not DirEntry objects), then
        # close the iterator before mutating.  Collecting the full list of
        # DirEntry objects would materialise stat info for every entry, which is
        # an OOM/DoS vector on directories with millions of files (HIGH #4).
        # Collecting names is cheap and safe: os.scandir over an fd is bounded
        # by the directory's actual entry count, and we only store strings.
        with os.scandir(entry_fd) as it:
            child_names = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
        for child_name, is_dir in child_names:
            if is_dir:
                _fd_anchored_rmtree(child_name, entry_fd, expected_st_dev)
            else:
                # Files, symlinks, special files -- unlink (no follow).
                try:
                    os.unlink(child_name, dir_fd=entry_fd)
                except FileNotFoundError:
                    pass
    finally:
        try:
            os.close(entry_fd)
        except OSError:
            pass

    # Now-empty directory -- remove via fd-anchored rmdir.
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _fd_anchored_phase1_rename(
    activated_repos_dir: str,
    username: str,
    user_alias: str,
) -> str:
    """Atomically rename ``{activated_repos_dir}/{username}/{user_alias}`` into
    ``{activated_repos_dir}/.trash/<trash_name>`` using fd-anchored syscalls.

    Story #1032 Commit 5 — closes BLOCKER #1 and BLOCKER #2 from the Codex
    re-review:

      BLOCKER #1: ``.trash`` can be swapped to a symlink between makedirs and
        os.rename.  Fix: open ``.trash`` with O_DIRECTORY|O_NOFOLLOW before
        calling rename, so a swap after the open is irrelevant — the kernel
        resolves the dst dir_fd, not the path.

      BLOCKER #2: ``{username}`` ancestor dir can be swapped to a symlink,
        redirecting the rename to an attacker-chosen source.  Fix: open
        ``{username}`` with O_DIRECTORY|O_NOFOLLOW relative to
        ``activated_repos_dir``, so the fd is pinned to the real inode
        regardless of later swaps.

    Both parent dirs are opened with O_DIRECTORY|O_NOFOLLOW before any rename
    syscall.  The final rename is issued as::

        os.rename(user_alias, trash_name,
                  src_dir_fd=user_fd, dst_dir_fd=trash_fd)

    which passes only basenames to the kernel — ancestor swaps after the fd
    opens cannot redirect the operation.

    Args:
        activated_repos_dir: Absolute path to the activated-repos root.  Must
            be a real directory (not a symlink) — opened with O_NOFOLLOW.
        username: Basename of the user directory.  Must not contain ``/``,
            ``\\``, ``..``, or null bytes.
        user_alias: Basename of the repo directory inside the user dir.
            Same basename constraints as *username*.

    Returns:
        The chosen ``trash_name`` basename (the new name under ``.trash/``).

    Raises:
        ValueError: Any safety invariant violated (symlink detected, bad arg,
            cross-filesystem target).
        OSError / FileNotFoundError: Kernel-level failure (missing directory,
            permissions, etc.).
    """
    # --- argument validation --------------------------------------------------
    for arg_name, arg_val in (("username", username), ("user_alias", user_alias)):
        if not arg_val or not isinstance(arg_val, str):
            raise ValueError(
                f"_fd_anchored_phase1_rename: refuse empty/non-string {arg_name}: {arg_val!r}"
            )
        if "\x00" in arg_val:
            raise ValueError(
                f"_fd_anchored_phase1_rename: null byte in {arg_name}: {arg_val!r}"
            )
        if "/" in arg_val or "\\" in arg_val:
            raise ValueError(
                f"_fd_anchored_phase1_rename: path separator in {arg_name}: {arg_val!r}"
            )
        if arg_val in (".", "..") or arg_val.startswith(".."):
            raise ValueError(
                f"_fd_anchored_phase1_rename: dot-dot in {arg_name}: {arg_val!r}"
            )

    # --- build trash_name -----------------------------------------------------
    trash_name = (
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        f"-{uuid.uuid4().hex[:8]}"
        f"-{username}-{user_alias}"
    )

    # --- open activated_repos_dir (top-level anchor) -------------------------
    try:
        repos_root_fd = os.open(
            activated_repos_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as e:
        raise ValueError(
            f"_fd_anchored_phase1_rename: cannot open activated_repos_dir "
            f"{activated_repos_dir!r} with O_NOFOLLOW: {e}"
        ) from e

    user_fd: Optional[int] = None
    trash_fd: Optional[int] = None
    try:
        # --- open username dir relative to repos_root_fd ---------------------
        try:
            user_fd = os.open(
                username,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=repos_root_fd,
            )
        except OSError as e:
            raise ValueError(
                f"_fd_anchored_phase1_rename: cannot open username dir "
                f"{username!r} with O_NOFOLLOW: {e}"
            ) from e

        # --- ensure .trash exists and open it with O_NOFOLLOW ----------------
        # Create if missing (mkdir relative to repos_root_fd).
        try:
            trash_fd = os.open(
                ".trash",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=repos_root_fd,
            )
        except FileNotFoundError:
            # .trash doesn't exist yet — create it atomically.
            try:
                os.mkdir(".trash", mode=0o700, dir_fd=repos_root_fd)
            except FileExistsError:
                pass  # race: another thread created it
            try:
                trash_fd = os.open(
                    ".trash",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=repos_root_fd,
                )
            except OSError as e2:
                raise ValueError(
                    f"_fd_anchored_phase1_rename: cannot open .trash after mkdir: {e2}"
                ) from e2
        except OSError as e:
            raise ValueError(
                f"_fd_anchored_phase1_rename: cannot open .trash with O_NOFOLLOW: {e}"
            ) from e

        # --- cross-filesystem guard ------------------------------------------
        user_dev = os.fstat(user_fd).st_dev
        trash_dev = os.fstat(trash_fd).st_dev
        if user_dev != trash_dev:
            raise ValueError(
                "_fd_anchored_phase1_rename: refuse cross-filesystem rename "
                f"(user st_dev={user_dev}, trash st_dev={trash_dev})"
            )

        # --- fd-anchored atomic rename ----------------------------------------
        # Both src and dst are basenames; both dir_fds are pinned inodes.
        # An ancestor swap after the os.open calls cannot redirect this.
        os.rename(user_alias, trash_name, src_dir_fd=user_fd, dst_dir_fd=trash_fd)

    finally:
        for fd in (repos_root_fd, user_fd, trash_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    return trash_name


def _predeactivation_leak_scan_enabled() -> bool:
    """Return True if the pre-deactivation leak scan is enabled (Story #1032 AC6).

    Reads the bootstrap config flag `enable_predeactivation_leak_scan`.  Default
    is False — pre-flight leak detection is OFF, so deactivation only pays the
    cost on the failure path.  Ops can flip the flag to True in config.json to
    restore pre-flight scanning during incident investigation.

    Returns False on any error so deactivation never fails because of telemetry.
    Tests patch this function directly via mock.patch.
    """
    try:
        from code_indexer.server.utils.config_manager import ServerConfigManager

        cfg = ServerConfigManager().load_config()
        return bool(getattr(cfg, "enable_predeactivation_leak_scan", False))
    except Exception:
        return False
