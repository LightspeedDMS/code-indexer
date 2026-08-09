"""
Git error classifier for fetch failure categorization.

Classifies git fetch stderr output into actionable categories so the refresh
scheduler can decide whether to immediately re-clone (corruption) or wait for
a threshold of consecutive failures (transient network/auth issues).

Story #295: Auto-Recovery for Corrupted Golden Repo Git Object Database.
"""

from typing import List


class GitFetchError(Exception):
    """
    Raised when git fetch fails with a classifiable error.

    Attributes:
        category: One of "permanent", "corruption", "transient", or "unknown".
        stderr: The raw stderr output from the failed git fetch command.
    """

    def __init__(self, message: str, category: str, stderr: str):
        super().__init__(message)
        self.category = category
        self.stderr = stderr


# Patterns indicating a permanent, non-recoverable access/existence failure
# (repository deleted, project renamed, credentials/access permanently
# revoked). No amount of retrying or re-cloning can resolve these --
# operator intervention (restore access, fix the URL) is required.
#
# Bug #1341: checked BEFORE TRANSIENT_PATTERNS. GitLab's permanent error
# ("The project you were looking for could not be found or you don't have
# permission to view it.") also emits a generic
# "fatal: Could not read from remote repository." line that would otherwise
# match the broader TRANSIENT_PATTERNS entry "Could not read from remote",
# causing a permanently-broken upstream to be endlessly retried/re-cloned.
PERMANENT_PATTERNS: List[str] = [
    "The project you were looking for could not be found",
    "you don't have permission",
    "Repository not found",
    "remote: Not Found",
    # Bug #1534, second Codex review round: WIDENED from the first round's
    # "'origin' does not appear to be a git repository" (which required the
    # literal quoted remote NAME) back to the bare tail phrase below.
    # Empirically confirmed via real `git fetch` subprocess repros (see
    # tests/unit/global_repos/test_git_error_classifier_1534.py) that this
    # exact wording is emitted by git in (at least) two structurally
    # distinct situations:
    #   Form 1 -- no "origin" remote configured at all, where the quoted
    #     string IS the remote name: "fatal: 'origin' does not appear to
    #     be a git repository".
    #   Form 2 -- an "origin" remote IS configured, but its URL resolves
    #     to a path that no longer exists / is not a valid git repository
    #     (e.g. a golden repo's origin pointing at a now-stale
    #     resolved_source_path). Here git quotes the RESOLVED PATH, not
    #     the literal string "origin": "fatal: '/some/stale/path' does
    #     not appear to be a git repository". The first round's exact
    #     "'origin'" quoting requirement never matched this real-world
    #     case, so it fell through to the broader TRANSIENT pattern
    #     "Could not read from remote" and was endlessly retried -- this
    #     is the more realistic real-world trigger for Bug #1534.
    # The first round's stated collision risk (a different remote name
    # co-occurring with genuinely transient wording) does not occur in
    # practice: real transient failures (Connection refused, Could not
    # resolve host, unable to access ...) were verified via real
    # subprocess repros to NEVER contain "does not appear to be a git
    # repository" -- git only emits that phrase for a local resolve-time
    # failure, a structurally different code path from a network
    # transport error. The phrase is also NOT specific to the "origin"
    # remote name -- any remote pointing at a non-repository path
    # produces the identical structural failure, and is equally
    # permanent regardless of the remote's name.
    "does not appear to be a git repository",
]

# Patterns indicating local object database corruption.
# These require immediate re-clone because the repo cannot self-heal.
CORRUPTION_PATTERNS: List[str] = [
    "Could not read",
    "pack has",
    "unresolved deltas",
    "invalid index-pack output",
    "is corrupt",
    "is empty",
    "packfile",
    "bad object",
]

# Patterns indicating transient failures (network, auth, DNS, SSH access).
# These may resolve on their own; re-clone only after repeated failures.
# NOTE: "Could not read from remote repository" is an SSH access error (transient),
# distinct from the corruption pattern "Could not read <object-hash>".
TRANSIENT_PATTERNS: List[str] = [
    "Could not read from remote",
    "Could not resolve host",
    "Connection refused",
    "Connection timed out",
    "Network is unreachable",
    "SSL",
    "unable to access",
    "Authentication failed",
]


def classify_fetch_error(stderr: str) -> str:
    """
    Classify a git fetch failure from its stderr output.

    Checks PERMANENT patterns first (Bug #1341): GitLab/GitHub access or
    existence errors (project not found, no permission, repository deleted)
    also emit a generic "Could not read from remote repository" line that
    would otherwise match the broader TRANSIENT pattern below -- but the
    failure is actually non-recoverable and must never be retried forever.

    Then checks transient patterns to prevent SSH access errors of the form
    "Could not read from remote repository" from being misclassified as
    corruption by the broader "Could not read" corruption pattern.
    Returns "unknown" when no known pattern matches.

    Args:
        stderr: The raw stderr string from the failed git fetch invocation.

    Returns:
        "permanent"  if the error indicates a non-recoverable access/existence issue.
        "corruption" if the error indicates local object database corruption.
        "transient"  if the error indicates a network or authentication issue.
        "unknown"    if the error does not match any known pattern.
    """
    for pattern in PERMANENT_PATTERNS:
        if pattern in stderr:
            return "permanent"

    for pattern in TRANSIENT_PATTERNS:
        if pattern in stderr:
            return "transient"

    for pattern in CORRUPTION_PATTERNS:
        if pattern in stderr:
            return "corruption"

    return "unknown"
