"""Shared "safe git argv" validation helpers.

Every git tool that builds a `git` subprocess argv from a caller-
controlled string routes that string through this module first: REST and
MCP push/pull/fetch/diff/log/blame/cat/show-commit/file-at-revision/
branch-create/branch-switch/branch-delete/reset/clean, across both
`GitOperationsService` implementations (`server/services/
git_operations_service.py` and `global_repos/git_operations.py`). Each of
those values (`remote`, `branch`, `from_revision`, `to_revision`,
`revision`, `commit_hash`, `source_branch`, `branch_name`, the legacy
`file_paths` list, `mode`) must be validated before it reaches argv,
against
exactly two hazards: a value git would read as an OPTION rather than a
literal (a positional argv element starting with `-`, other than exactly
`-`), and an argv-breaking character (NUL, CR, LF, or another C0 control
character -- ordinary spaces and tabs are not a hazard, since argv is
never parsed by a shell). Nothing else is checked here: a remote must
additionally be one of the repository's actually-configured remotes (so
a caller cannot name an arbitrary URL or filesystem path as the
destination), but beyond that, git itself resolves or rejects a value
exactly as it would with no validation at all.

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

import subprocess
from pathlib import Path
from typing import Iterable, List, Optional

from code_indexer.utils.git_runner import run_git_command

# cidx's own untracked working paths inside a repository clone: the index
# directory and the project-level override file. Unanchored patterns, so
# they are kept at any depth.
CIDX_OWNED_WORKING_PATHS = (".code-indexer", ".code-indexer-override.yaml")

# The ONE argv every server-side "remove untracked files" runs (REST and
# MCP git_clean, and the pre-refresh clearing of a dirty repository). Each
# `-e` adds an ignore rule, and without `-x` git clean keeps ignored paths,
# so the repository's index survives the clean.
GIT_CLEAN_UNTRACKED_ARGV = ["git", "clean", "-fd"] + [
    arg for path in CIDX_OWNED_WORKING_PATHS for arg in ("-e", path)
]

# Every uncommitted change, tracked and untracked. Untracked files are
# listed individually so `uncommitted_status_lines` can recognise cidx's
# own working paths inside an otherwise-collapsed untracked directory.
GIT_STATUS_UNCOMMITTED_ARGV = [
    "git",
    "status",
    "--porcelain",
    "--untracked-files=all",
]

_UNTRACKED_PREFIX = "?? "


def _is_untracked_cidx_working_path(line: str) -> bool:
    if not line.startswith(_UNTRACKED_PREFIX):
        return False
    path = line[len(_UNTRACKED_PREFIX) :]
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    return any(part in CIDX_OWNED_WORKING_PATHS for part in path.split("/"))


def uncommitted_status_lines(status_stdout: str) -> List[str]:
    """The porcelain lines of `GIT_STATUS_UNCOMMITTED_ARGV` that are real
    local changes.

    Only UNTRACKED entries under cidx's own working paths (the ones the
    clean above keeps) are dropped, so they never make a repository look
    dirty; a TRACKED change to those paths (M/A/D/R/...) is kept.
    """
    return [
        line
        for line in status_stdout.splitlines()
        if line.strip() and not _is_untracked_cidx_working_path(line)
    ]


class GitArgumentValidationError(ValueError):
    """Raised when a caller-supplied git argv component fails safety validation.

    Subclasses ValueError deliberately -- see module docstring.
    """


def reject_leading_dash(value: str, *, param_name: str) -> None:
    """Reject any value starting with '-'.

    Any value starting with '-' would be parsed by git as an option
    rather than a literal value, so it is rejected here before any `git`
    subprocess is even constructed.
    """
    if value.startswith("-"):
        raise GitArgumentValidationError(
            f"Invalid {param_name}: value must not start with '-' (it would "
            f"be parsed as a git option rather than a literal value): "
            f"{value!r}"
        )


def validate_remote_syntax(remote: str) -> str:
    """Apply the two hazard checks to `remote` without any subprocess.

    Rejects a value starting with '-' (other than exactly `-`, which is a
    single character and never an option; git accepts it as a remote
    name) and a value containing a NUL/CR/LF/other C0 control character.
    An empty string passes through unchanged. Used on its own where a
    remote name reaches a git argv before `validate_remote_name`'s
    membership check can run -- e.g. the MCP git_push handler's `git
    remote get-url <remote>` preflight -- and as the first step of
    `validate_remote_name` itself.
    """
    if remote == "-":
        return remote
    reject_leading_dash(remote, param_name="remote")
    _reject_control_characters(remote, param_name="remote")
    return remote


def validate_remote_name(remote: str, repo_path: Path) -> str:
    """Validate `remote` is a safe, actually-configured remote name.

    An empty string is not one of the two hazards this module checks (it
    can never be read as a git OPTION), so it passes through unchanged
    here -- exactly as it did with no validation at all, before this
    function existed. Each call site's own pre-existing argument handling
    (e.g. `git fetch`/`git push`/`git pull`, which each build their argv
    from `remote` unconditionally) decides what an empty remote means.

    Raises GitArgumentValidationError if a NON-EMPTY `remote` fails
    `validate_remote_syntax`, or is not exactly one of the repository's
    remotes as listed by `git remote`. The membership requirement keeps a
    caller from naming an arbitrary URL or filesystem path as the
    destination; any name git itself accepts for a configured remote
    (e.g. `a+b`, or exactly `-`) is otherwise usable.

    Returns the validated remote name unchanged.
    """
    if not remote:
        return remote
    validate_remote_syntax(remote)

    from code_indexer.utils import git_runner

    try:
        result = run_git_command(
            ["git", "remote"],
            cwd=repo_path,
            check=True,
            timeout=git_runner.REMOTE_RESOLVE_TIMEOUT_SECONDS,
        )
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


def _reject_control_characters(value: str, *, param_name: str) -> None:
    """Reject a value containing NUL, CR, LF, or another C0 control
    character other than tab.

    Ordinary spaces and tabs are never a hazard here: this value is
    passed to `git` as one literal argv element, never parsed by a shell,
    so embedded spaces cannot be split into separate arguments. NUL
    truncates a C string; CR and LF (and the other C0 controls) could
    otherwise let a value read differently at a later boundary than the
    one just validated here (e.g. a log line, or a caller's own string
    comparison).
    """
    for c in value:
        if c == "\t":
            continue
        if ord(c) < 0x20:
            raise GitArgumentValidationError(
                f"Invalid {param_name} {value!r}: must not contain a NUL, "
                f"CR, LF, or other control character"
            )


def validate_branch_name(
    branch: Optional[str],
    *,
    param_name: str = "branch",
) -> Optional[str]:
    """Validate an optional branch/ref name or push/pull refspec.

    `None` passes through unchanged (branch is optional in push/pull). An
    empty string is not one of the two hazards either (it can never be
    read as a git OPTION), so it too passes through unchanged here --
    exactly as it did with no validation at all, before this function
    existed; each call site's own pre-existing truthy check (e.g. `if
    branch:`) or git itself decides what an empty value means. The exact
    value `-` also passes through unchanged for every caller: it is a
    single character, not `-<something>`, so git never reads it as an
    option (e.g. `git checkout -` is git's own previous-branch shorthand;
    `git push <remote> -` and `git branch -` let git itself resolve or
    reject it). Raises GitArgumentValidationError if `branch` starts with
    '-' and is not exactly `-`, or contains a NUL/CR/LF/other C0
    control character (see `_reject_control_characters`). These are the
    only two hazards checked: a leading '-' would be read as a git OPTION
    rather than a literal value, and a control character could let git or
    a later boundary read the value differently than validated here.
    Nothing else is checked -- git itself resolves or rejects anything
    else exactly as it would with no validation at all.

    The value is always ONE argv element, so only the whole element is
    checked. Its contents are ref syntax that git itself interprets: a
    push/pull refspec's leading '+' force marker and its `src:dst` sides
    (e.g. `+main`, `HEAD~1:refs/heads/older`, `main:-`, `:refs/heads/old`)
    and `@{-1}` on branch switch all reach git unchanged, since an argv
    element that does not itself start with '-' is never an option.
    """
    if branch is None:
        return None
    if branch == "-":
        return branch
    reject_leading_dash(branch, param_name=param_name)
    _reject_control_characters(branch, param_name=param_name)
    return branch


def validate_revision(
    revision: Optional[str],
    repo_path: Path,
    *,
    param_name: str,
) -> Optional[str]:
    """Validate an optional revision expression (commit/tag/branch/HEAD~N/etc).

    A pure delegation to `validate_revision_range`: both apply the
    identical check to a single argv element (a leading '-' that is not
    exactly `-`, or a NUL/CR/LF/other C0 control character), and sharing
    the one implementation keeps them from drifting apart -- there is no
    behavior left for this function to add on top. `repo_path` is
    passed straight through for signature compatibility with call sites
    that pass it positionally; it is unused by either function (there is
    no `git rev-parse` resolution in this module).

    `None` and an empty string both pass through unchanged -- neither is
    one of the two hazards, so each call site's own pre-existing truthy
    check or resolution logic (or git itself) decides what either value
    means, exactly as it did with no validation at all, before this
    function existed.

    This validator's name distinguishes call sites where a two-dot/three-
    dot range is not meaningful from `validate_revision_range` (git diff,
    git log, git blame), which accept one; both apply the identical
    check via the delegation above.
    """
    return validate_revision_range(revision, repo_path, param_name=param_name)


def validate_revision_range(
    revision: Optional[str], repo_path: Path, *, param_name: str
) -> Optional[str]:
    """Validate a revision expression that may be a single revision or a
    two-dot/three-dot RANGE, as `git diff`/`git log`/`git blame` all
    accept.

    `None` passes through unchanged. An empty string is not one of the
    two hazards either (it can never be read as a git OPTION), so it too
    passes through unchanged here -- exactly as it did with no validation
    at all, before this function existed; each call site's own
    pre-existing truthy check or git itself decides what an empty value
    means. The exact value `-` is also not an option: it is a single
    character, not `-<something>`, so there is no OPTION reading of it
    for git to be tricked into. Raises GitArgumentValidationError if
    `revision` starts with '-' and is not exactly `-`, or contains a
    NUL/CR/LF/other C0 control character. Nothing else is checked: a
    revision or range is passed to `git` as ONE literal argv element, so
    the only hazard is the whole value being read as an option (a leading
    '-' followed by more characters) -- `main..-x` is not an option,
    since it starts with 'm', and git itself rejects it (with its own
    "unknown revision" error) exactly as it would with no validation at
    all. This also means a single revision that happens to CONTAIN '..'
    -- e.g. `:/fix foo..bar` (commit-message search) or
    `HEAD^{/fix foo..bar}` (commit-message search from a base) -- is not
    misread as a range: there is no splitting here for either case to
    collide with.

    `repo_path` is accepted for signature compatibility with call sites
    that pass it positionally; it is unused (there is no `git rev-parse`
    resolution here -- see module docstring).
    """
    del repo_path
    if revision is None:
        return None
    if revision == "" or revision == "-":
        return revision
    reject_leading_dash(revision, param_name=param_name)
    _reject_control_characters(revision, param_name=param_name)
    return revision


def validate_pathspecs(file_paths: Optional[Iterable[str]]) -> Optional[List[str]]:
    """Validate a legacy `file_paths` pathspec list.

    Unlike a revision or branch name, a pathspec is never passed to `git`
    on its own: every real caller of this function (`git_stage`'s `git
    add -- ...`, `git_unstage`'s `git reset HEAD -- ...`, and
    `git_diff`'s legacy `file_paths` branch, which itself emits `--`
    before extending the argv) already places a `--` end-of-options
    marker before these paths reach argv. A leading '-' -- including a
    real filename that is literally "-" -- can therefore never be
    misread as a git option here, and is not a hazard worth checking.
    Ordinary CR/LF are real, legal filesystem-path characters too (a
    filename may legitimately contain one), so they are not rejected
    either. `None` passes through unchanged.

    Raises GitArgumentValidationError if any entry contains a NUL byte,
    which is the one thing that is never a legal filesystem-path
    character and would otherwise reach `subprocess.run` directly and
    raise a bare `ValueError` ("embedded null byte") instead of this
    validator's own clean `GitArgumentValidationError`.
    """
    if file_paths is None:
        return None
    validated: List[str] = []
    for p in file_paths:
        if "\x00" in p:
            raise GitArgumentValidationError(
                f"Invalid file_paths entry: value must not contain a NUL byte: {p!r}"
            )
        validated.append(p)
    return validated


# The exact literal mode strings this server's `git reset` support
# accepts. Shared by both the REST route and the MCP tool so `git reset`
# always validates its `mode` argument against the same allowlist before
# any confirmation-token logic runs.
#
# A literal-equality allowlist is required here, not merely a
# leading-dash rejection: git's own long-option parser accepts any
# unambiguous PREFIX of a long option name (e.g. `--har` resolves to
# `--hard`, verified empirically against real git 2.52), so a caller
# value like "har" -- which does not start with '-' and does not equal
# the literal string "hard" -- would silently perform a real hard reset
# while never triggering the mode == "hard" confirmation-token branch
# that gates it.
VALID_GIT_RESET_MODES = frozenset({"soft", "mixed", "hard", "keep", "merge"})


def validate_reset_mode(mode: str) -> str:
    """Validate `mode` is exactly one of VALID_GIT_RESET_MODES, or the
    empty string.

    An empty string is not an option and passes through unchanged: the
    call site's own pre-existing `f"--{mode}"` command construction turns
    it into a bare `--` separator, so the argv is `git reset -- <target>`
    -- no mode flag, and the target (e.g. `HEAD`) is read by git as a
    PATHSPEC after `--`, not as a commit. That argv reaches git exactly as
    it did with no validation at all, and git itself decides the outcome
    (for a target that matches no path, git exits 0 and changes nothing).

    Raises GitArgumentValidationError for any OTHER value that is not
    exactly one of VALID_GIT_RESET_MODES (a leading-dash check alone is
    not enough: git's own long-option parser accepts any unambiguous
    prefix of a long option name, so this allowlist still applies in
    full to every non-empty value). Returns `mode` unchanged on success.
    """
    if mode == "":
        return mode
    if mode not in VALID_GIT_RESET_MODES:
        raise GitArgumentValidationError(
            f"Invalid reset mode {mode!r}: must be exactly one of "
            f"{sorted(VALID_GIT_RESET_MODES)}"
        )
    return mode
