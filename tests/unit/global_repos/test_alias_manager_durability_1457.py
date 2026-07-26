"""AliasManager parent-directory-fsync durability hardening (Story #1457 AC10).

Both AliasManager.create_alias and AliasManager.swap_alias currently do
flush -> nfs_safe_fsync(file fd) -> os.replace(tmp_path, alias_file) but never
fsync the PARENT aliases directory afterward -- unlike the full durable-rename
pattern established by id_index_manager.py:203-212. This story makes both
primitives more heavily-used, load-bearing publication mechanisms (every
temporal refresh's atomic publish, AC6/AC11), so both gain the missing
parent-directory fsync.

These tests use a real filesystem (tmp_path) and a spy WRAPPER around the
real ``nfs_safe_fsync`` utility (never a mock/stub of AliasManager's own
logic) to observe that at least one fsync call targets a DIRECTORY file
descriptor, proving the parent-directory fsync actually happens -- not just
that the alias file content is correct (which passed even before this fix).
"""

import os
import stat

from code_indexer.global_repos import alias_manager as alias_manager_module
from code_indexer.global_repos.alias_manager import AliasManager


def _install_fd_kind_spy(monkeypatch):
    """Wrap the REAL nfs_safe_fsync to record whether each fsync'd fd is a directory.

    Calls through to the original implementation (never a mock) so on-disk
    durability behavior is unchanged; only observes fd kind.
    """
    calls = []
    original = alias_manager_module.nfs_safe_fsync

    def spy(fd):
        try:
            is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        except OSError:
            is_dir = None
        calls.append(is_dir)
        return original(fd)

    monkeypatch.setattr(alias_manager_module, "nfs_safe_fsync", spy)
    return calls


def test_create_alias_fsyncs_parent_aliases_directory(tmp_path, monkeypatch):
    calls = _install_fd_kind_spy(monkeypatch)

    manager = AliasManager(str(tmp_path))
    manager.create_alias("myrepo-global", "/some/target/path")

    assert True in calls, (
        "create_alias must fsync the parent aliases directory after "
        f"os.replace (AC10) -- observed fsync fd kinds: {calls}"
    )


def test_swap_alias_fsyncs_parent_aliases_directory(tmp_path, monkeypatch):
    manager = AliasManager(str(tmp_path))
    manager.create_alias("myrepo-global", "/old/target")

    calls = _install_fd_kind_spy(monkeypatch)
    manager.swap_alias("myrepo-global", "/new/target", "/old/target")

    assert True in calls, (
        "swap_alias must fsync the parent aliases directory after "
        f"os.replace (AC10) -- observed fsync fd kinds: {calls}"
    )
