"""Shared "safe git argv" validation helpers.

`GitOperationsService.git_push`, `git_pull`, `git_fetch`, and `git_diff`
build a `git` subprocess argv from caller-controlled strings (`remote`,
`branch`, `from_revision`, `to_revision`, the legacy `file_paths` list).
Git parses any argument starting with `-` as an OPTION, not a literal
value, so every one of these values must be validated before it reaches
argv: a remote must be one of the repository's actually-configured
remotes, and no remote/branch/revision/pathspec value may start with `-`
(branch additionally may not start with `+`, git's own force-push ref
marker).

This module is the ONE shared validation layer all of those call sites go
through, so a caller can never construct an argv containing an option it
did not intend -- independent of, and strictly stronger than, the
`--end-of-options` defense-in-depth also added at each call site (see
git_operations_service.py).

All validators raise `GitArgumentValidationError` (a `ValueError` subclass)
on rejection. `ValueError` is the same exception type
`GitOperationsService` already uses for other caller-input validation (e.g.
`git_commit`'s email/name format checks), so both the REST routers (which
already map `ValueError` -> HTTP 400 for sibling validation errors) and the
MCP handlers (which already catch broad `Exception` and return a
structured `{"success": False, "error": ...}` response) handle it without
any new wiring.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional

from code_indexer.utils.git_runner import run_git_command

# Deliberately strict: alphanumeric, '.', '_', '-' only, and must NOT start
# with '-' (enforced again explicitly below for a clearer error message).
# This matches how real git remote names look in practice (origin, golden,
# upstream, my-fork) while rejecting anything that could be misread as an
# option by git itself.
#
# No anchors here on purpose: this is matched with `.fullmatch(...)`, not
# `.match(...)`. A `^...$`-anchored pattern combined with `.match()` has a
# trailing-newline class of bug -- Python's `$` matches just before
# a FINAL trailing newline, not only true end-of-string, so
# `re.match(r"^[A-Za-z0-9]+$", "origin\n")` incorrectly returns a match.
# `.fullmatch()` requires the ENTIRE string to satisfy the pattern with no
# such leniency, which is what "exact identifier, no anchors needed" means
# in Python's re module.
_REMOTE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class GitArgumentValidationError(ValueError):
    """Raised when a caller-supplied git argv component fails safety validation.

    Subclasses ValueError deliberately -- see module docstring.
    """


def reject_leading_dash(value: str, *, param_name: str) -> None:
    """Reject any value starting with '-'.

    Any value starting with '-' would be parsed by git as an option
    rather than a literal value, so it is rejected here before any `git`
    subprocess is even constructed.

    Public (not `_`-prefixed) because it is also used standalone by
    callers that need ONLY this syntactic, no-subprocess guard before a
    git-argv-building call -- e.g. mcp/handlers/git_write.py's git_push
    handler, immediately before `_get_pat_credential_for_remote()`'s own
    `git remote get-url <remote>` call -- without requiring `repo_path` to
    already be a real, initialized git repository the way the full
    `validate_remote_name()` membership check below does (that stronger
    check still runs, unconditionally, inside
    GitOperationsService.git_push_with_pat before any of its own
    subprocesses; this function alone is defense-in-depth at an earlier
    call site).
    """
    if value.startswith("-"):
        raise GitArgumentValidationError(
            f"Invalid {param_name}: value must not start with '-' (it would "
            f"be parsed as a git option rather than a literal value): "
            f"{value!r}"
        )


def validate_remote_name(remote: str, repo_path: Path) -> str:
    """Validate `remote` is a safe, actually-configured remote name.

    Raises GitArgumentValidationError if `remote` is empty, starts with
    '-', does not match the strict identifier pattern, or is not one of
    the repository's remotes actually configured via `git remote`.

    Returns the validated remote name unchanged.
    """
    if not remote:
        raise GitArgumentValidationError("remote name cannot be empty")
    reject_leading_dash(remote, param_name="remote")
    if not _REMOTE_NAME_PATTERN.fullmatch(remote):
        raise GitArgumentValidationError(
            f"Invalid remote name {remote!r}: must match "
            f"{_REMOTE_NAME_PATTERN.pattern!r} (alphanumeric, '.', '_', '-'; "
            "cannot start with '-')"
        )

    try:
        result = run_git_command(["git", "remote"], cwd=repo_path, check=True)
    except subprocess.CalledProcessError as e:
        raise GitArgumentValidationError(
            f"Unable to list configured remotes for repository: {e}"
        )

    configured = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if remote not in configured:
        raise GitArgumentValidationError(
            f"Remote {remote!r} is not a configured remote for this "
            f"repository (configured remotes: {sorted(configured)})"
        )
    return remote


def validate_branch_name(
    branch: Optional[str], *, param_name: str = "branch"
) -> Optional[str]:
    """Validate an optional branch/ref name.

    `None` passes through unchanged (branch is optional in push/pull).
    Raises GitArgumentValidationError if `branch` is an empty string,
    starts with '-' or '+', or fails `git check-ref-format --allow-onelevel`.

    Note: this validator has no Python regex of its own -- well-formedness
    is delegated entirely to `git check-ref-format`, which is itself
    already strict about a trailing '\\n'/'\\r' (verified empirically
    against real git 2.52: `check-ref-format` rejects both with
    returncode 1) AND about a `src:dst` refspec (verified empirically:
    `check-ref-format --allow-onelevel 'a:b'` returns returncode 1). See
    TestValidateBranchName's trailing-newline and colon-refspec regression
    tests in test_git_argv_safety.py.

    A leading '+' is the one case `check-ref-format` does NOT reject
    (verified empirically: `check-ref-format --allow-onelevel '+main'`
    returns returncode 0) -- git's own `+<ref>` force-push marker syntax,
    which `git push <remote> +main` honors even behind `--end-of-options`
    (verified empirically: the push still reports "(forced update)"). This
    is checked explicitly here, before check-ref-format ever runs.
    """
    if branch is None:
        return None
    if branch == "":
        raise GitArgumentValidationError(f"{param_name} cannot be an empty string")
    reject_leading_dash(branch, param_name=param_name)
    if branch.startswith("+"):
        raise GitArgumentValidationError(
            f"Invalid {param_name}: value must not start with '+' (git's "
            f"force-push ref marker -- '+<ref>' bypasses --end-of-options "
            f"and rewrites the remote branch): {branch!r}"
        )

    result = subprocess.run(
        ["git", "check-ref-format", "--allow-onelevel", branch],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitArgumentValidationError(
            f"Invalid {param_name} {branch!r}: not a well-formed git ref name"
        )
    return branch


def validate_revision(
    revision: Optional[str], repo_path: Path, *, param_name: str
) -> Optional[str]:
    """Validate an optional revision expression (commit/tag/branch/HEAD~N/etc).

    `None` passes through unchanged (from_revision/to_revision are
    optional in git_diff except that from_revision is required at the
    MCP-handler level, not here). Raises GitArgumentValidationError if
    `revision` is an empty string, starts with '-', or does not resolve
    via `git rev-parse --verify --end-of-options` against `repo_path`.

    Note: this validator has no Python regex of its own -- resolution is
    delegated entirely to `git rev-parse --verify`, which is itself
    already strict about a trailing '\\n'/'\\r' (verified empirically
    against real git 2.52: `rev-parse --verify` rejects both with
    "fatal: Needed a single revision", returncode 128). See
    TestValidateRevision's trailing-newline regression test in
    test_git_argv_safety.py.
    """
    if revision is None:
        return None
    if revision == "":
        raise GitArgumentValidationError(f"{param_name} cannot be an empty string")
    reject_leading_dash(revision, param_name=param_name)

    try:
        run_git_command(
            ["git", "rev-parse", "--verify", "--end-of-options", revision],
            cwd=repo_path,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise GitArgumentValidationError(
            f"Invalid {param_name} {revision!r}: does not resolve to a "
            f"valid git revision: {e}"
        )
    return revision


def validate_pathspecs(file_paths: Optional[Iterable[str]]) -> Optional[List[str]]:
    """Validate a legacy `file_paths` pathspec list.

    `None` passes through unchanged. Raises GitArgumentValidationError if
    any entry starts with '-'. Callers are still responsible for emitting
    `--` before the returned list when building argv (defense in depth;
    see git_operations_service.git_diff).
    """
    if file_paths is None:
        return None
    validated: List[str] = []
    for p in file_paths:
        if p.startswith("-"):
            raise GitArgumentValidationError(
                f"Invalid file_paths entry: value must not start with '-' "
                f"(it would be parsed as a git option rather than a "
                f"literal pathspec): {p!r}"
            )
        validated.append(p)
    return validated
