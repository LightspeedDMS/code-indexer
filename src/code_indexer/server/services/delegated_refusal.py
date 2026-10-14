"""
Features that need the repository on this pod: refused, loudly, when delegated.

With ``content_backend: delegated`` the repositories are not on this pod's disk
-- workspace-mcp holds the files and git-mcp the history. Most features read
them through those seams. A few have no seam to read through yet (the operation
is not one workspace-mcp or git-mcp exposes); run against this pod's disk they
would see an empty directory and answer "no files", "no history", "not a git
repository" -- a wrong answer that looks like a right one.

Those features call :func:`refuse_when_delegated` before touching the disk. On
a delegated pod it raises :class:`DelegatedRepositoryUnsupportedError` naming
the feature and why; everywhere else (the local backend, the CLI, tests without
a running server) it does nothing and the feature runs exactly as before.

``dep_map_delegated`` is the dependency-map instance of the same rule.
"""

from __future__ import annotations

from typing import Optional

UNSUPPORTED_SUFFIX = (
    "is not supported with content_backend='delegated': this pod does not hold "
    "the repository"
)


class DelegatedRepositoryUnsupportedError(RuntimeError):
    """A feature that reads the repository from this pod's disk, on a pod
    whose content backend is delegated (the repository is elsewhere)."""

    def __init__(self, feature: str, reason: str) -> None:
        super().__init__(f"{feature} {UNSUPPORTED_SUFFIX} ({reason})")
        self.feature = feature
        self.reason = reason


def delegated_refusal(feature: str, reason: str) -> Optional[str]:
    """The refusal message for *feature* on this server, or None when it can run."""
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated,
    )

    if not is_content_seam_delegated():
        return None
    return str(DelegatedRepositoryUnsupportedError(feature, reason))


def refuse_when_delegated(feature: str, reason: str) -> None:
    """Raise DelegatedRepositoryUnsupportedError when the repository is not here.

    Call it before the first local read of the repository, so nothing partial
    is produced or written.
    """
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated,
    )

    if is_content_seam_delegated():
        raise DelegatedRepositoryUnsupportedError(feature, reason)
