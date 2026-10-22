"""Bulk repository access for one group (mixed into GroupAccessManager).

Each batch is ONE groups-store transaction: a failure leaves no partial
grant or revocation.  Audit rows live in a separate store, so they are
written only after the batch committed.  A repository name here is the
golden-repo alias.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .constants import CIDX_META_REPO
from code_indexer.server.services.audit_outcome import audit_target_id

if TYPE_CHECKING:
    from .group_access_manager import Group

_GRANT = "repo_access_grant"
_REVOKE = "repo_access_revoke"


class RepoAccessBatchMixin:
    """Batch grant/revoke of many repositories for one group."""

    _backend: Any
    _conn_manager: Any

    if TYPE_CHECKING:

        def get_group(self, group_id: int) -> Optional[Group]: ...

        def _record_group_outcome(
            self,
            actor: str,
            action_type: str,
            target_type: str,
            verified_target: Optional[str],
            outcome: str,
            details: Optional[Dict[str, Any]],
        ) -> None: ...

    def grant_repos_access(
        self, repo_names: List[str], group_id: int, granted_by: str
    ) -> List[str]:
        """Grant every name in one transaction; return the newly granted names.

        Raises:
            ValueError: If the group does not exist.
        """
        if self._backend is not None:
            return list(
                self._backend.grant_repos_access(repo_names, group_id, granted_by)
            )
        if self.get_group(group_id) is None:
            raise ValueError(f"Group with ID {group_id} not found")
        now = datetime.now(timezone.utc).isoformat()

        def _do_grant(conn: sqlite3.Connection) -> List[str]:
            cursor = conn.cursor()
            granted: List[str] = []
            for name in repo_names:
                cursor.execute(
                    "INSERT OR IGNORE INTO repo_group_access "
                    "(repo_name, group_id, granted_at, granted_by) VALUES (?, ?, ?, ?)",
                    (name, group_id, now, granted_by),
                )
                if cursor.rowcount > 0:
                    granted.append(name)
            return granted

        return list(self._conn_manager.execute_atomic(_do_grant))

    def revoke_repos_access(self, repo_names: List[str], group_id: int) -> List[str]:
        """Revoke every name in one transaction; return the revoked names.

        Raises:
            CidxMetaCannotBeRevokedError: If cidx-meta is among the names.
        """
        from .group_access_manager import CidxMetaCannotBeRevokedError

        if CIDX_META_REPO in repo_names:
            raise CidxMetaCannotBeRevokedError(
                f"{CIDX_META_REPO} access cannot be revoked from any group"
            )
        if self._backend is not None:
            return list(self._backend.revoke_repos_access(repo_names, group_id))

        def _do_revoke(conn: sqlite3.Connection) -> List[str]:
            cursor = conn.cursor()
            revoked: List[str] = []
            for name in repo_names:
                cursor.execute(
                    "DELETE FROM repo_group_access WHERE repo_name = ? AND group_id = ?",
                    (name, group_id),
                )
                if cursor.rowcount > 0:
                    revoked.append(name)
            return revoked

        return list(self._conn_manager.execute_atomic(_do_revoke))

    def _existing_group(self, group_id: int) -> Group:
        from .group_access_manager import GroupNotFoundError

        group = self.get_group(group_id)
        if group is None:
            raise GroupNotFoundError(f"Group with ID {group_id} not found")
        return group

    def _record_successes(
        self, actor: str, action: str, names: List[str], group: Group
    ) -> None:
        for name in names:
            target = audit_target_id("repo", name)
            details = {"repo": target, "group": group.name}
            self._record_group_outcome(
                actor, action, "repo", target, "success", details
            )

    def grant_repos_access_audited(
        self, repo_names: List[str], group_id: int, *, actor: str
    ) -> List[str]:
        """Grant many repositories to a group; return the newly granted names.

        cidx-meta and duplicates are skipped.  One ``success`` row per newly
        granted repository, written after the grants committed; names already
        granted write nothing.  A missing group or a failed batch (nothing
        granted) writes one ``failure`` row and the exception propagates.

        Raises:
            GroupNotFoundError: *group_id* names no group.
        """
        names = [n for n in dict.fromkeys(repo_names) if n != CIDX_META_REPO]
        try:
            group = self._existing_group(group_id)
            granted = self.grant_repos_access(names, group_id, actor) if names else []
        except Exception:
            self._record_group_outcome(actor, _GRANT, "repo", None, "failure", None)
            raise
        self._record_successes(actor, _GRANT, granted, group)
        return granted

    def revoke_repos_access_audited_aliases(
        self, repo_names: List[str], group_id: int, *, actor: str
    ) -> List[str]:
        """Revoke many repositories from a group; return the revoked names.

        cidx-meta and duplicates are skipped.  One ``success`` row per revoked
        repository (as the single-repository door writes), written after the
        batch committed; the requested names that were not in the group write
        ONE ``failure`` row between them carrying only their count.  A missing
        group, or a failed batch (nothing revoked), writes one ``failure`` row
        and the exception propagates.

        Raises:
            GroupNotFoundError: *group_id* names no group.
        """
        unique = [n for n in dict.fromkeys(repo_names) if n != CIDX_META_REPO]
        try:
            group = self._existing_group(group_id)
            revoked = self.revoke_repos_access(unique, group_id) if unique else []
        except Exception:
            self._record_group_outcome(actor, _REVOKE, "repo", None, "failure", None)
            raise
        self._record_successes(actor, _REVOKE, revoked, group)
        not_in_group = len(unique) - len(revoked)
        if not_in_group:
            summary = {"group": group.name, "not_in_group_count": not_in_group}
            self._record_group_outcome(actor, _REVOKE, "repo", None, "failure", summary)
        return revoked

    def revoke_repos_access_audited(
        self, repo_names: List[str], group_id: int, *, actor: str
    ) -> int:
        """Count form of :meth:`revoke_repos_access_audited_aliases`."""
        return len(
            self.revoke_repos_access_audited_aliases(repo_names, group_id, actor=actor)
        )
