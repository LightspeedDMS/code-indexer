"""Regression tests for Bug #1610.

_ForwardingModule.__setattr__ (in handlers/__init__.py) mirrors every
package-level attribute write into a hardcoded list of domain submodules,
including handlers.xray, whenever the submodule's __dict__ already contains
a same-named entry -- regardless of whether that entry is genuinely the same
shared object or merely an independently-defined, coincidentally-named local
symbol.

xray.py defines its own local `_resolve_repo_path(alias: str)` (1-arg),
completely unrelated to the package-level `_resolve_repo_path` re-exported
from `_legacy.py` (`_resolve_repo_path(repo_identifier, golden_repos_dir)`,
2-arg). Because both happen to share the same name, any
`patch("code_indexer.server.mcp.handlers._resolve_repo_path", ...)` --
even one that has nothing to do with xray.py -- gets its *restore* step
(mock.patch's plain `setattr` on exit) mirrored into `xray.__dict__`,
permanently replacing xray's own 1-arg function with the incompatible
2-arg `_legacy` one for the rest of the pytest process.

test_cidx_meta_directory_tree_access.py's `_call_handle` helper does exactly
this patch (to mock its own, unrelated external dependencies -- golden repos
dir resolution and access filtering). Running any of its tests must NOT, as
a side effect, corrupt xray's own binding.

Both tests below exercise `_ForwardingModule.__setattr__` -- the actual
system under test -- through its own real public interface (plain attribute
assignment/lookup via `setattr`/`getattr`, the same mechanism mock.patch uses
internally), not through `unittest.mock.patch`.

Note: the culprit test class is imported under a private alias
(`_CulpritTestClass`) so pytest's default `Test*` collection does not also
pick it up and execute it independently as part of *this* module -- that
would corrupt xray's binding before our own test body even runs, confounding
the result. The pristine reference to xray's real function is captured at
MODULE IMPORT TIME (during pytest's collection phase, which happens for
every test module before any test body anywhere executes), so the comparison
below is always against a guaranteed-untouched original, regardless of
cross-file test execution order in a full suite run.
"""

import code_indexer.server.mcp.handlers.xray as xray

from .test_cidx_meta_directory_tree_access import (
    TestDirectoryTreeCidxMetaAccessFiltering as _CulpritTestClass,
)

# xray.py's own _resolve_repo_path(alias: str) takes exactly one argument,
# unlike _legacy's _resolve_repo_path(repo_identifier, golden_repos_dir).
_XRAY_RESOLVE_REPO_PATH_ARGCOUNT = 1

# Captured at collection time, before any test body (in any module) has run,
# so this is guaranteed to be xray's real, uncorrupted local function.
_ORIGINAL_XRAY_RESOLVE_REPO_PATH = getattr(xray, "_resolve_repo_path")


def test_xray_resolve_repo_path_survives_handlers_patch_restore(
    regular_user, access_filtering_service
) -> None:
    """Running the #336 culprit test must not clobber xray's own _resolve_repo_path.

    Bug #1610: before the fix, running
    TestDirectoryTreeCidxMetaAccessFiltering.test_regular_user_sees_no_repo_files_in_tree
    (which patches "code_indexer.server.mcp.handlers._resolve_repo_path" and lets
    mock.patch restore it on exit) replaces xray's own _resolve_repo_path
    binding with the unrelated 2-arg _legacy function, breaking every later
    test that calls an xray.py handler path reaching the module-local 1-arg
    _resolve_repo_path(alias).
    """
    assert (
        _ORIGINAL_XRAY_RESOLVE_REPO_PATH.__code__.co_argcount
        == _XRAY_RESOLVE_REPO_PATH_ARGCOUNT
    ), (
        "sanity check: xray's own _resolve_repo_path must be the 1-arg local "
        "function -- captured pristine at collection time"
    )

    test_instance = _CulpritTestClass()
    test_instance.test_regular_user_sees_no_repo_files_in_tree(
        regular_user, access_filtering_service
    )

    assert getattr(xray, "_resolve_repo_path") is _ORIGINAL_XRAY_RESOLVE_REPO_PATH, (
        "xray's own _resolve_repo_path(alias) was clobbered by the unrelated "
        "package-level _legacy._resolve_repo_path(repo_identifier, "
        "golden_repos_dir) forwarding restore -- Bug #1610"
    )
    assert (
        getattr(xray, "_resolve_repo_path").__code__.co_argcount
        == _XRAY_RESOLVE_REPO_PATH_ARGCOUNT
    ), "xray's _resolve_repo_path must still be the 1-arg local function"


def test_legitimate_shared_name_still_forwards_and_restores() -> None:
    """A name that IS genuinely shared across the forwarding list must still work.

    _get_golden_repos_dir is imported as the *same* function object (from
    _utils) by the package, repos.py, search.py, and files.py -- a real
    alias, not a coincidental name collision. Writing it at the package
    level must still propagate into those submodules, and restoring it must
    still restore their bindings too.
    """
    import code_indexer.server.mcp.handlers as handlers
    import code_indexer.server.mcp.handlers.repos as repos
    import code_indexer.server.mcp.handlers.search as search
    import code_indexer.server.mcp.handlers.files as files

    original = getattr(handlers, "_get_golden_repos_dir")
    # Sanity: this name really is a shared alias (same object) everywhere.
    assert getattr(repos, "_get_golden_repos_dir") is original
    assert getattr(search, "_get_golden_repos_dir") is original
    assert getattr(files, "_get_golden_repos_dir") is original

    def _replacement_golden_repos_dir() -> str:
        return "/sentinel/golden/repos/dir"

    setattr(handlers, "_get_golden_repos_dir", _replacement_golden_repos_dir)
    try:
        assert getattr(repos, "_get_golden_repos_dir") is _replacement_golden_repos_dir
        assert getattr(search, "_get_golden_repos_dir") is _replacement_golden_repos_dir
        assert getattr(files, "_get_golden_repos_dir") is _replacement_golden_repos_dir
    finally:
        setattr(handlers, "_get_golden_repos_dir", original)

    # Restored correctly in every submodule after the write is undone.
    assert getattr(repos, "_get_golden_repos_dir") is original
    assert getattr(search, "_get_golden_repos_dir") is original
    assert getattr(files, "_get_golden_repos_dir") is original
