"""What one caller may see of the internal catalog repository.

``MetaVisibility`` is resolved once per request and never cached across
requests: a grant change is visible on the very next call.

Admin means membership of the ``admins`` group (``is_admin_user``), never a
role or a permission. An administrator acting for other users (MCP
``acting_users``) is restricted to that scope, which the dispatcher passes
through the ``ACTING_SCOPE`` context variable. Only the MCP dispatcher
(``mcp/protocol.py:_dispatch_tool_call``) sets it, for one handler call;
the scope is never stored on a module, class or session object.
"""

from __future__ import annotations

import functools
import hashlib
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    FrozenSet,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Tuple,
)

from ..constants import CIDX_META_REPO, is_internal_meta_repo
from ..repo_access_guard import normalize_repo_alias

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService

UNRESTRICTED_VIS_HASH = "unrestricted"
UNRESOLVED_IDENTITY = "<unresolved>"

ACTING_SCOPE: ContextVar[Optional[FrozenSet[str]]] = ContextVar(
    "cidx_meta_acting_scope", default=None
)


@dataclass(frozen=True)
class MetaVisibility:
    """The caller's view: everything (admins) or a set of golden repo names.

    ``visible`` holds bare golden names (no ``-global``) and never the
    catalog itself. ``vis_hash`` is equal for equal visible sets, so caches
    keyed by it are shared per group, never per user.
    """

    unrestricted: bool
    visible: FrozenSet[str]
    vis_hash: str

    def __post_init__(self) -> None:
        if self.unrestricted and self.visible:
            raise ValueError("an unrestricted visibility carries no visible set")
        if any(is_internal_meta_repo(name) for name in self.visible):
            raise ValueError("a visible set never contains the catalog repository")
        if any(name.endswith("-global") for name in self.visible):
            raise ValueError("visible names are bare golden names")

    @functools.cached_property
    def visible_lc(self) -> FrozenSet[str]:
        """``visible`` lowercased: the form dependency-map membership is
        compared in."""
        return frozenset(name.lower() for name in self.visible)

    @classmethod
    def everything(cls) -> "MetaVisibility":
        return cls(
            unrestricted=True, visible=frozenset(), vis_hash=UNRESTRICTED_VIS_HASH
        )

    @classmethod
    def restricted(cls, repos: Iterable[str]) -> "MetaVisibility":
        visible = frozenset(
            normalize_repo_alias(repo)
            for repo in repos
            if isinstance(repo, str) and repo
        )
        visible = frozenset(name for name in visible if not is_internal_meta_repo(name))
        digest = hashlib.sha256("\n".join(sorted(visible)).encode("utf-8")).hexdigest()
        return cls(unrestricted=False, visible=visible, vis_hash=digest)


def resolve_meta_visibility(
    username: str, access_service: Optional[Any]
) -> MetaVisibility:
    """Resolve the caller's catalog visibility, fresh, failing closed.

    Order: a missing access service yields an empty visible set; an
    acting-user scope restricts to that scope; an ``admins`` group member is
    unrestricted; anyone else sees their granted repositories.
    """
    if access_service is None:
        return MetaVisibility.restricted(())
    scope = ACTING_SCOPE.get()
    is_admin = access_service.is_admin_user(username)
    if scope is not None:
        scoped = MetaVisibility.restricted(scope)
        if is_admin:
            return scoped
        own = MetaVisibility.restricted(access_service.get_accessible_repos(username))
        return MetaVisibility.restricted(scoped.visible & own.visible)
    if is_admin:
        return MetaVisibility.everything()
    return MetaVisibility.restricted(access_service.get_accessible_repos(username))


def unrestricted_caller(
    username: str, access_service: Optional["AccessFilteringService"]
) -> bool:
    """True exactly when ``resolve_meta_visibility`` would be unrestricted,
    without reading the caller's grants: an ``admins`` member with no
    acting-user scope. A missing access service is never unrestricted."""
    return (
        access_service is not None
        and ACTING_SCOPE.get() is None
        and access_service.is_admin_user(username)
    )


def memory_visibility_fn(
    access_service: Optional["AccessFilteringService"], vis: MetaVisibility
) -> Callable[[str], bool]:
    """The ``memory_fn`` for ``classify``: the memory scope rule
    (AccessFilteringService.memory_visible_within, frontmatter read fresh)
    judged against the caller's visible set plus the catalog itself, which
    every group is granted -- the same set memory retrieval uses."""
    if vis.unrestricted:
        return lambda _memory_id: True
    if access_service is None:
        return lambda _memory_id: False
    service = access_service
    accessible = set(vis.visible) | {CIDX_META_REPO}
    return lambda memory_id: service.memory_visible_within(memory_id, accessible)


def names_activation(alias: str) -> bool:
    """True when *alias* can name one of the caller's activations: a bare
    alias, not ``-global`` and not shaped like a path."""
    if alias.endswith("-global") or alias in (".", ".."):
        return False
    return not any(ch in alias for ch in "/\\\x00")


def activation_identities(record: Mapping[str, Any]) -> Tuple[str, ...]:
    """Golden repositories an activation record was created from (every
    component for a composite); ``UNRESOLVED_IDENTITY`` when none is
    recorded."""
    if record.get("is_composite"):
        golden = [str(g) for g in (record.get("golden_repo_aliases") or []) if g]
    else:
        single = record.get("golden_repo_alias")
        golden = [str(single)] if single else []
    return tuple(golden) if golden else (UNRESOLVED_IDENTITY,)


def meta_identity(repo_info: Mapping[str, Any]) -> Tuple[str, ...]:
    """Golden identity of one searched repository entry (per-hit detection).

    Global entries are their own alias; an explicit ``repo_path`` entry is
    its ``actual_repo_id``; an activation is the golden repository (every
    component for a composite) it was created from, never its own
    ``user_alias``. An entry whose source cannot be determined yields
    ``UNRESOLVED_IDENTITY``.
    """
    if repo_info.get("is_global"):
        return (str(repo_info["user_alias"]),)
    if repo_info.get("repo_path"):
        actual = repo_info.get("actual_repo_id") or repo_info.get("user_alias")
        return (str(actual),) if actual else (UNRESOLVED_IDENTITY,)
    return activation_identities(repo_info)


def request_alias_identities(
    alias: str, username: str, activated_repo_manager: Optional[Any]
) -> Tuple[str, ...]:
    """Golden identity behind a request alias, for doors that have only it.

    A ``-global`` or path-shaped alias is its own identity. A bare alias is
    the caller's activation when one has that ``user_alias`` (its golden
    source), else a golden name. Without the activation manager the alias
    cannot be resolved: ``UNRESOLVED_IDENTITY``.
    """
    if not names_activation(alias):
        return (alias,)
    if activated_repo_manager is None:
        return (UNRESOLVED_IDENTITY,)
    try:
        record = activated_repo_manager.get_repository(username, alias, touch=False)
    except Exception as exc:
        logger.warning(
            "Activation lookup for catalog detection failed (%s); treating the "
            "alias as the catalog",
            type(exc).__name__,
        )
        return (UNRESOLVED_IDENTITY,)
    if not isinstance(record, Mapping):
        return (alias,)
    return activation_identities(record)


def is_internal_meta_identity(identities: Iterable[str]) -> bool:
    """True when any identity is the catalog or could not be resolved."""
    resolved = tuple(identities)
    if not resolved:
        return True
    return any(i == UNRESOLVED_IDENTITY or is_internal_meta_repo(i) for i in resolved)


@contextmanager
def acting_scope(repos: Optional[Iterable[str]]) -> Iterator[None]:
    """Bind the acting-user scope (None = no scope) for the enclosed call."""
    value = None if repos is None else frozenset(repos)
    token = ACTING_SCOPE.set(value)
    try:
        yield
    finally:
        ACTING_SCOPE.reset(token)
