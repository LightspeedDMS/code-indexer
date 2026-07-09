"""Bare-to-global alias fallback helper for READ-ONLY MCP handlers (Story #1039).

This module provides a single helper, ``try_global_fallback``, that transparently
promotes a bare repository alias (e.g. ``"evolution"``) to its globally-activated
form (``"evolution-global"``) when:

  1. The alias does not already end with ``"-global"``.
  2. The user does NOT have the alias in their own activated-repo list.
  3. The golden repository is globally active.

FORBIDDEN IMPORTERS
-------------------
This module MUST NOT be imported from any write, git-mutation, PR, CI/CD, or
provider-index handler.  Permitted callers are limited to the 31 read-only
handlers listed in Story #1039 Section A:

  * search/intelligence handlers: search_code, handle_regex_search,
    get_file_content, list_files, browse_directory, directory_tree,
    handle_xray_search, handle_xray_explore, handle_xray_dump_ast
  * SCIP handlers: scip_definition, scip_references, scip_dependencies,
    scip_dependents, scip_impact, scip_callchain, scip_context
  * repos handler: get_branches
  * git read handlers: git_log, git_blame, git_diff, git_show_commit,
    git_file_history, git_file_at_revision, git_search_commits,
    git_search_diffs, git_status, git_fetch, git_branch_list,
    git_conflict_status

DO NOT import this module from:
  create_file, edit_file, delete_file, git_commit, git_amend, git_merge,
  git_stash, git_clean, git_reset, git_checkout_file, git_stage,
  git_unstage, git_mark_resolved, git_merge_abort, git_branch_create,
  git_branch_delete, git_branch_switch, git_push, git_pull, git_fetch
  (write path), create_pull_request, close_pull_request, update_pull_request,
  merge_pull_request, comment_on_pull_request, ci_* handlers,
  manage_provider_indexes, trigger_reindex, get_index_status, check_health,
  activate_repository, deactivate_repository, switch_branch.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


def try_global_fallback(
    alias: Optional[str],
    golden_repo_manager: "GoldenRepoManager",
) -> Optional[str]:
    """Return the ``-global`` form of *alias* when the golden repo is globally active.

    This helper is called by read-only handlers AFTER confirming that the
    authenticated user does NOT have *alias* in their own activated-repo list.
    It must NOT be called when the user already has the repo activated -- the
    activated-repo takes precedence over the global form.

    Args:
        alias: The repository alias supplied by the caller.  May be ``None``,
            an empty string, a bare alias (``"evolution"``), or an already-suffixed
            alias (``"evolution-global"``).
        golden_repo_manager: Live ``GoldenRepoManager`` instance used to query
            global activation status.

    Returns:
        ``"<alias>-global"`` when all conditions are met; ``None`` otherwise.

    Conditions that cause ``None`` to be returned (short-circuit order):
        * *alias* is ``None`` or empty.
        * *alias* already ends with ``"-global"`` (caller should use it as-is).
        * The golden repository for *alias* is not globally active.
    """
    if not alias:
        return None
    if alias.endswith("-global"):
        return None
    if golden_repo_manager.is_globally_active(alias):
        return f"{alias}-global"
    return None
