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
        category: One of "corruption", "transient", or "unknown".
        stderr: The raw stderr output from the failed git fetch command.
    """

    def __init__(self, message: str, category: str, stderr: str):
        super().__init__(message)
        self.category = category
        self.stderr = stderr


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

    Checks transient patterns first to prevent SSH access errors of the form
    "Could not read from remote repository" from being misclassified as
    corruption by the broader "Could not read" corruption pattern.
    Returns "unknown" when no known pattern matches.

    Args:
        stderr: The raw stderr string from the failed git fetch invocation.

    Returns:
        "corruption" if the error indicates local object database corruption.
        "transient"  if the error indicates a network or authentication issue.
        "unknown"    if the error does not match any known pattern.
    """
    for pattern in TRANSIENT_PATTERNS:
        if pattern in stderr:
            return "transient"

    for pattern in CORRUPTION_PATTERNS:
        if pattern in stderr:
            return "corruption"

    return "unknown"
