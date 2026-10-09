"""An account's activated repositories never outlive it under its name.

Deleting an account submits removal of every repository it activated, through
the same audited path an admin uses to remove another user's repository
(background jobs).  A name cannot be created again while any repository of
the earlier account remains (listed activation or a non-empty per-user
activation directory), so a new account can never adopt an old clone.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

logger = logging.getLogger(__name__)

LEFTOVERS_MESSAGE = "previous account's repositories are still being removed"


class AccountActivations:
    """Removal and leftover detection of the repositories a name activated."""

    def __init__(self, activated_repo_manager: "ActivatedRepoManager") -> None:
        if activated_repo_manager is None:
            raise ValueError("activated_repo_manager is required")
        self._arm = activated_repo_manager

    def submit_removal(self, username: str, *, actor: str) -> List[str]:
        """Submit removal of every repository *username* activated.

        Each repository is submitted independently; a refused submission is
        logged at ERROR and the rest still go ahead (the leftovers keep the
        name from being created again).  Returns the submitted job ids.
        """
        from code_indexer.server.services.activated_repo_audited_ops import (
            deactivate_repository_for_user,
        )

        job_ids: List[str] = []
        for repo in self._arm.list_activated_repositories(username):
            alias = repo.get("user_alias", "")
            try:
                job_ids.append(
                    deactivate_repository_for_user(
                        self._arm, username, alias, actor=actor
                    )
                )
            except Exception as exc:  # noqa: BLE001 - account already deleted
                logger.error(
                    "Removing repository %r of deleted account %r failed: %s",
                    alias,
                    username,
                    exc,
                    exc_info=True,
                )
        return job_ids

    def has_leftovers(self, username: str) -> bool:
        """True while any repository activated under *username* remains."""
        if self._arm.list_activated_repositories(username):
            return True
        user_dir = os.path.join(self._arm.activated_repos_dir, username)
        if not os.path.isdir(user_dir):
            return False
        with os.scandir(user_dir) as entries:
            return any(True for _ in entries)

    def ensure_name_free(self, username: str) -> None:
        """Refuse creating *username* while an earlier account's repositories
        remain (raises ``ValueError``)."""
        if self.has_leftovers(username):
            raise ValueError(LEFTOVERS_MESSAGE)
