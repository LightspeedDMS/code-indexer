"""
OrphanedRepoError - typed exception for an orphaned golden-repo clone.

Bug #1338 (follow-up to #1336): #1336 made lifecycle_backfill /
global_repo_refresh skip orphaned golden aliases (registry row present,
on-disk clone directory absent) by matching the raised error MESSAGE TEXT
across module boundaries. That is brittle -- a reworded message silently
breaks orphan-skip.

This module defines a single, dedicated exception type raised at BOTH
orphaned-clone source sites:
  - LifecycleClaudeCliInvoker._validate_repo_inputs (missing repo_path for a
    registered alias)
  - GitPullUpdater.__init__ (missing clone directory)

and caught BY TYPE at the two skip sites (lifecycle_batch_runner.py,
refresh_scheduler.py) -- no string matching involved.

OrphanedRepoError subclasses ValueError so any existing `except ValueError`
call site continues to work unchanged.
"""

from __future__ import annotations


class OrphanedRepoError(ValueError):
    """
    Raised when a registered golden-repo alias's on-disk clone directory does
    not exist (an "orphan": registry row present, filesystem clone absent).

    This is deliberately narrow: it must be raised ONLY for the missing-clone
    condition, never for other input-validation failures (empty alias, None
    path, path-exists-but-not-a-directory, or genuine upstream failures) --
    those must remain plain ValueError / other exception types so they still
    propagate as real per-alias failures at the skip sites.
    """
