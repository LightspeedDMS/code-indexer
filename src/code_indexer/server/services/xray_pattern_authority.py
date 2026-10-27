"""Who may write which X-Ray pattern: one rule for every door.

``store_xray_pattern`` and the catalog file tools (create, edit, delete on
the catalog in write mode) both decide here:

- a repository scope (``xray-patterns/<alias>/``) needs a grant on that
  golden repository (an ``admins`` member is granted every one);
- replacing or deleting a shared ``__any__`` pattern needs the admin role;
- creating a new shared pattern needs neither.

Synchronous (group store reads): async callers use a worker thread.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional

from .cidx_meta_access.path_policy import (
    XRAY_ANY_SCOPE,
    XRAY_PATTERNS_DIR,
    normalize_meta_path,
)

if TYPE_CHECKING:
    from ..auth.user_manager import User

SHARED_PATTERN_REFUSAL = (
    "Replacing or deleting a shared X-Ray pattern requires the admin role."
)


@dataclass(frozen=True)
class PatternWriter:
    """The caller of a pattern write: *access* is the access filtering
    service (None refuses every repository scope)."""

    username: str
    admin_role: bool
    access: Optional[Any]

    @classmethod
    def of(cls, user: "User", access: Optional[Any]) -> "PatternWriter":
        from ..auth.user_manager import UserRole

        return cls(user.username, user.role == UserRole.ADMIN, access)


def may_write_scope(scope: str, writer: PatternWriter) -> bool:
    """True when *writer* may write patterns of *scope*: ``__any__`` names
    no repository; any other scope is judged by golden repository name,
    like the dispatcher's repository guard."""
    from .access_filtering_service import AccessFilteringService

    if scope == XRAY_ANY_SCOPE:
        return True
    access = writer.access
    return access is not None and (
        access.is_admin_user(writer.username)
        or AccessFilteringService.alias_granted(
            scope, access.get_accessible_repos(writer.username), {}
        )
    )


def may_replace_pattern(scope: str, *, admin_role: bool) -> bool:
    """True when an existing pattern of *scope* may be replaced or deleted
    by a caller holding (*admin_role*) or lacking the admin role."""
    return scope != XRAY_ANY_SCOPE or admin_role


# A path under the pattern directory that cannot be normalised (no real
# scope is empty).
_UNJUDGEABLE = ""


def _pattern_scope(root: str, target: Path) -> Optional[str]:
    """The pattern scope *target* lies in under the catalog *root* (real
    path), ``_UNJUDGEABLE`` for an unnormalisable path under the pattern
    directory, or None when it is no pattern file."""
    relative = Path(os.path.relpath(str(target), root)).as_posix()
    parts = normalize_meta_path(relative)
    if parts is None:
        under = relative.split("/", 1)[0] == XRAY_PATTERNS_DIR
        return _UNJUDGEABLE if under else None
    if len(parts) < 3 or parts[0] != XRAY_PATTERNS_DIR:
        return None
    return parts[1]


def refuse_catalog_pattern_write(
    catalog_root: Path,
    targets: Iterable[Path],
    *,
    replacing: bool,
    writer: Optional[PatternWriter],
) -> None:
    """Refuse a file-tool write into the catalog *catalog_root* touching any
    pattern file among *targets* (the paths actually written or removed)
    unless *writer* may: *replacing* = an existing file is changed or
    deleted. No writer refuses every pattern file.

    Raises:
        PermissionError: ``CATALOG_WRITE_REFUSAL`` for an ungranted scope, an
            unnormalisable pattern path or a missing writer;
            ``SHARED_PATTERN_REFUSAL`` for a shared pattern replaced without
            the admin role.
    """
    from .cidx_meta_access.file_doors import CATALOG_WRITE_REFUSAL

    root = os.path.realpath(catalog_root)
    for target in targets:
        scope = _pattern_scope(root, target)
        if scope is None:
            continue
        if (
            writer is None
            or scope == _UNJUDGEABLE
            or not may_write_scope(scope, writer)
        ):
            raise PermissionError(CATALOG_WRITE_REFUSAL)
        if replacing and not may_replace_pattern(scope, admin_role=writer.admin_role):
            raise PermissionError(SHARED_PATTERN_REFUSAL)
