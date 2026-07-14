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
