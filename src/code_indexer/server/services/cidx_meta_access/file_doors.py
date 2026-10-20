"""What a file door may list or read of the internal catalog for one request.

A door judges the request once (``judge_file_door``) against the directory
it actually serves and hands the result to the file service:

- ``UNRESTRICTED`` -- an admin, or a directory that is not the catalog:
  served unchanged;
- a ``CatalogFileView`` -- a restricted caller on the catalog (named so, or
  served from one of its locations whatever the alias or activation record
  says).

With a view every path is classified before anything is read: a denied
path raises exactly the reader's not-found error; descriptions and
dependency-map documents are projected line-aligned, so offsets address
projected lines; memories and allowed files are served as they are.
Listings keep what the policy allows, visible dependency-map documents
included. The file service refuses a catalog directory when a request
carries no judgement at all (``refuse_unjudged_catalog``).

Synchronous (stores and files): async callers use a worker thread.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Optional,
)

from ...cache.handle_binding import bind_handles, reset_handle_binding
from .catalog_requests import is_catalog_request
from .depmap_projection import MetaProjector, render_line_aligned
from .history_policy import _may_serve_catalog
from .membership import participants_reader
from .path_policy import (
    DEP_MAP_DIR,
    Decision,
    DecisionKind,
    classify,
    may_list_under,
)
from .served_identity import path_is_catalog_location, path_looks_like_catalog
from .universe_provider import process_alias_universe, registry_of
from .visibility import (
    MetaVisibility,
    memory_visibility_fn,
    resolve_meta_visibility,
    unrestricted_caller,
)

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService

LinesView = Callable[[List[str]], List[str]]
_PROJECTED = frozenset({DecisionKind.DESCRIPTION, DecisionKind.TRANSFORM})


def not_found(relative_path: str) -> FileNotFoundError:
    """The reader's error for a missing file (FileListingService)."""
    return FileNotFoundError(f"File not found: {relative_path}")


@dataclass
class CatalogFileView:
    """A restricted caller's view of the catalog served from *root*."""

    vis: MetaVisibility
    root: Path
    access_service: Optional[Any]
    registry: Any
    _participants: Dict[str, Optional[FrozenSet[str]]] = field(default_factory=dict)
    _projector: Optional[MetaProjector] = None

    def _members(self, domain: str) -> Optional[FrozenSet[str]]:
        if domain not in self._participants:
            read = participants_reader(self.root / DEP_MAP_DIR)
            self._participants[domain] = read(domain)
        return self._participants[domain]

    def decide(self, relative_path: str) -> Decision:
        """The policy decision; everything is denied without the access
        service."""
        if self.access_service is None:
            return Decision(DecisionKind.DENY)
        memory_fn = memory_visibility_fn(self.access_service, self.vis)
        return classify(relative_path, self.vis, self._members, memory_fn)

    def _canonical(self, relative_path: str) -> Optional[str]:
        """The catalog-relative path of the file actually served for
        *relative_path* (symlinks and dot segments resolved), or None when
        it lies outside the catalog or is the catalog itself."""
        if not isinstance(relative_path, str) or "\x00" in relative_path:
            return None
        root = os.path.realpath(self.root)
        real = os.path.realpath(os.path.join(root, relative_path))
        if not real.startswith(root + os.sep):
            return None
        return Path(os.path.relpath(real, root)).as_posix()

    def _served_decision(self, relative_path: str) -> Decision:
        canonical = self._canonical(relative_path)
        if canonical is None:
            return Decision(DecisionKind.DENY)
        return self.decide(canonical)

    def listable(self, relative_path: str) -> bool:
        return self._served_decision(relative_path).kind is not DecisionKind.DENY

    def may_list_under(self, relative_path: str) -> bool:
        memory_fn = memory_visibility_fn(self.access_service, self.vis)
        return may_list_under(relative_path, self.vis, memory_fn)

    def lines_view(self, relative_path: str) -> Optional[LinesView]:
        """The view of *relative_path*'s lines (None: served as they are).

        Raises:
            FileNotFoundError: the path is denied (the missing-file error).
        """
        canonical = self._canonical(relative_path)
        decision = self._served_decision(relative_path)
        if decision.kind is DecisionKind.DENY or canonical is None:
            raise not_found(relative_path)
        if decision.kind not in _PROJECTED:
            return None
        return lambda lines: self._project(lines, canonical)

    def _project(self, lines: List[str], canonical: str) -> List[str]:
        if self._projector is None:
            self._projector = MetaProjector(
                self.vis,
                dep_map_dir=self.root / DEP_MAP_DIR,
                universe=process_alias_universe(self.registry),
            )
        bare = [line[:-1] if line.endswith("\n") else line for line in lines]
        shown = render_line_aligned(self._projector.project(bare, canonical))
        rendered = shown.split("\n")
        return [
            text + ("\n" if line.endswith("\n") else "")
            for text, line in zip(rendered, lines)
        ]

    def project_text(self, relative_path: str, text: str) -> str:
        """*text* (the whole file) as the caller may see it.

        Raises:
            FileNotFoundError: the path is denied.
        """
        view = self.lines_view(relative_path)
        if view is None:
            return text
        return "".join(view(text.splitlines(keepends=True)))


@dataclass(frozen=True)
class CatalogFileAccess:
    """One request's judgement; ``view`` None = serve unchanged. ``served``
    is the directory judged (None only for an admin): the file service
    serves nothing from any other directory under this judgement."""

    view: Optional[CatalogFileView]
    served: Optional[str] = None

    def served_text(self, relative_path: str, text: str) -> str:
        """*text* (the whole file at *relative_path*) as the caller may see
        it. Raises FileNotFoundError for a denied path."""
        if self.view is None:
            return text
        return self.view.project_text(relative_path, text)

    def lists(self, relative_path: str, is_dir: bool) -> bool:
        """True when a listing may show this entry; a directory when the
        caller could see something under it (the root always)."""
        if self.view is None or relative_path in ("", "."):
            return True
        if not is_dir:
            return self.view.listable(relative_path)
        canonical = self.view._canonical(relative_path)
        return canonical is not None and self.view.may_list_under(canonical)


UNRESTRICTED = CatalogFileAccess(None)


def judge_app_request(
    state: Any,
    alias: Any,
    served_path: Optional[str],
    username: str,
    activated_repo_manager: Optional[Any],
) -> CatalogFileAccess:
    """``judge_file_door`` with the stores read from the server *state*; a
    golden directory that is not a non-empty string counts as missing."""
    golden = getattr(state, "golden_repos_dir", None)
    return judge_file_door(
        alias,
        served_path,
        username,
        getattr(state, "access_filtering_service", None),
        activated_repo_manager,
        golden if isinstance(golden, str) and golden else None,
        registry_of(state),
    )


def _served_from_catalog(served_path: str, golden_repos_dir: Optional[str]) -> bool:
    if path_looks_like_catalog(served_path):
        return True
    if not golden_repos_dir:
        return True
    return path_is_catalog_location(served_path, golden_repos_dir)


def judge_file_door(
    alias: Any,
    served_path: Optional[str],
    username: str,
    access_service: Optional["AccessFilteringService"],
    activated_repo_manager: Optional[Any],
    golden_repos_dir: Optional[str],
    registry: Any,
) -> CatalogFileAccess:
    """Judge a request serving *served_path* under *alias*.

    Only an admin, or a request whose alias is not the catalog AND whose
    served directory is confirmed not to be a catalog location, is
    unrestricted. An alias the gate cannot judge (not a non-empty string, a
    glob, an empty collection) and a missing golden repositories directory
    count as the catalog; a missing access service restricts.

    Raises:
        FileNotFoundError: a restricted request whose served directory is
            unknown.
    """
    if unrestricted_caller(username, access_service):
        return UNRESTRICTED
    if not served_path:
        raise FileNotFoundError("Repository not found")
    catalog = _may_serve_catalog(
        alias, lambda name: is_catalog_request(name, username, activated_repo_manager)
    ) or _served_from_catalog(served_path, golden_repos_dir)
    if not catalog:
        return CatalogFileAccess(None, served_path)
    vis = resolve_meta_visibility(username, access_service)
    return CatalogFileAccess(
        CatalogFileView(vis, Path(served_path), access_service, registry),
        served_path,
    )


def served_tree_view(
    visibility: Optional[MetaVisibility],
    served_path: str,
    username: str,
    access_service: Optional["AccessFilteringService"],
    golden_repos_dir: Optional[str],
    registry: Any,
) -> Optional[CatalogFileView]:
    """The view for a door that already resolved *visibility* for the
    request (None = not judged the catalog by name or activation): reused
    as is, else a restricted caller served from a catalog location gets one.
    No activation or grant is read twice."""
    if visibility is None:
        if unrestricted_caller(username, access_service):
            return None
        if not _served_from_catalog(served_path, golden_repos_dir):
            return None
        visibility = resolve_meta_visibility(username, access_service)
    if visibility.unrestricted:
        return None
    return CatalogFileView(visibility, Path(served_path), access_service, registry)


def view_for(
    repo_path: str, access: Optional[CatalogFileAccess], missing: Exception
) -> Optional[CatalogFileView]:
    """The view the file service applies to *repo_path*.

    Raises:
        *missing*: no judgement was passed and the path looks like the
            catalog, or the judgement was made for another directory
            (fail closed).
    """
    if access is None:
        if path_looks_like_catalog(repo_path):
            raise missing
        return None
    if access.served is not None and os.path.realpath(
        access.served
    ) != os.path.realpath(repo_path):
        raise missing
    return access.view


@contextmanager
def bound_handles(access: CatalogFileAccess) -> Iterator[None]:
    """Bind payload handles minted while serving to the caller's
    visibility when a catalog view applies (revocation re-checks them)."""
    binding = access.view.vis.vis_hash if access.view is not None else None
    token: "Token[Optional[str]]" = bind_handles(binding)
    try:
        yield
    finally:
        reset_handle_binding(token)
