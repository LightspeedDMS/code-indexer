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

Writes (``refuse_hidden_catalog_writes``) judge the canonical path before
any existence check or read: a restricted caller may write only paths served
unchanged (allowed files, memories); every other path gets one fixed refusal
whether or not it exists.

Synchronous (stores and files): async callers use a worker thread.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import Token
from dataclasses import dataclass
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
from .membership import ParticipantsReader, request_participants_reader
from .path_policy import (
    DEP_MAP_DIR,
    Decision,
    DecisionKind,
    classify,
    may_list_under,
)
from .served_identity import (
    activation_is_composite,
    composite_component_paths,
    path_looks_like_catalog,
    request_catalog_locations,
    served_path_is_catalog,
)
from .universe_provider import process_alias_universe, registry_of
from .visibility import (
    MetaVisibility,
    memory_visibility_fn,
    resolve_meta_visibility,
    unrestricted_caller,
)

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService

logger = logging.getLogger(__name__)

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
    _participants: Optional[ParticipantsReader] = None
    _projector: Optional[MetaProjector] = None

    def _meta_projector(self) -> MetaProjector:
        """The request's projector; its model also answers membership."""
        if self._projector is None:
            self._projector = MetaProjector(
                self.vis,
                dep_map_dir=self.root / DEP_MAP_DIR,
                universe=process_alias_universe(self.registry),
            )
        return self._projector

    def _members(self, domain: str) -> Optional[FrozenSet[str]]:
        if self._participants is None:
            self._participants = request_participants_reader(
                self.root / DEP_MAP_DIR, lambda: self._meta_projector().model()
            )
        return self._participants(domain)

    def decide(self, relative_path: str) -> Decision:
        """The policy decision; everything is denied without the access
        service."""
        if self.access_service is None:
            return Decision(DecisionKind.DENY)
        memory_fn = memory_visibility_fn(self.access_service, self.vis)
        return classify(relative_path, self.vis, self._members, memory_fn)

    def _relative_real(self, relative_path: str) -> Optional[str]:
        """The catalog-relative path actually served for *relative_path*
        (symlinks and dot segments resolved; ``""`` for the catalog itself),
        or None when it lies outside the catalog."""
        if not isinstance(relative_path, str) or "\x00" in relative_path:
            return None
        root = os.path.realpath(self.root)
        real = os.path.realpath(os.path.join(root, relative_path))
        if real == root:
            return ""
        if not real.startswith(root + os.sep):
            return None
        return Path(os.path.relpath(real, root)).as_posix()

    def _canonical(self, relative_path: str) -> Optional[str]:
        """``_relative_real``, None for the catalog itself."""
        return self._relative_real(relative_path) or None

    def tree_root(self, relative_path: str) -> Optional[str]:
        """The canonical root a tree of *relative_path* walks (``""`` for
        the catalog itself), or None when the caller may see nothing there:
        a directory by what could be listed under it, a document by the file
        decision, a missing path by either (the walk then answers the same
        not-found)."""
        canonical = self._relative_real(relative_path)
        if not canonical:
            return canonical
        target = self.root / canonical
        as_dir = not target.exists() or target.is_dir()
        as_file = not target.is_dir()
        if (as_dir and self.may_list_under(canonical)) or (
            as_file and self.listable(canonical)
        ):
            return canonical
        return None

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
        bare = [line[:-1] if line.endswith("\n") else line for line in lines]
        shown = render_line_aligned(self._meta_projector().project(bare, canonical))
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

BINARY_SNIFF_BYTES = 8192


def served_file_payload(
    path: str, file_path: Path, access: CatalogFileAccess
) -> Dict[str, Any]:
    """The content payload of the confined, judged file *file_path* (requested
    as *path*): binary (a NUL byte, or not UTF-8, in the first
    ``BINARY_SNIFF_BYTES``, or not UTF-8 at all) has no content; text is
    served as *access* allows, and under a catalog view its ``size`` is that
    of the served text.

    Raises:
        FileNotFoundError: the judgement denies *path*.
        OSError: the file cannot be read.
    """
    size = file_path.stat().st_size
    binary: Dict[str, Any] = {
        "path": path,
        "is_binary": True,
        "size": size,
        "content": None,
    }
    with open(file_path, "rb") as f:
        chunk = f.read(BINARY_SNIFF_BYTES)
    if b"\x00" in chunk:
        return binary
    try:
        chunk.decode("utf-8")
        raw = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        logger.debug("Serving a file that is not UTF-8 as binary")
        return binary
    text = access.served_text(path, raw)
    if access.view is not None:
        size = len(text.encode("utf-8"))
    return {"path": path, "is_binary": False, "size": size, "content": text}


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


def _served_from_catalog(
    served_path: str, golden_repos_dir: Optional[str], composite_activation: bool
) -> bool:
    """True when *served_path*, or any component of a composite served there,
    is the catalog or cannot be judged. A *composite_activation* (its record
    says so) whose configuration names no component cannot be judged."""
    components = composite_component_paths(served_path)
    if components is None or (composite_activation and not components):
        return True
    if not golden_repos_dir:
        return True
    locations = request_catalog_locations(golden_repos_dir)
    return any(
        served_path_is_catalog(path, golden_repos_dir, locations)
        for path in [served_path, *components]
    )


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
    ) or _served_from_catalog(
        served_path,
        golden_repos_dir,
        activation_is_composite(alias, username, activated_repo_manager),
    )
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
    *,
    composite_activation: bool,
) -> Optional[CatalogFileView]:
    """The view for a door that already resolved *visibility* for the
    request (None = not judged the catalog by name or activation): reused
    as is, else a restricted caller served from a catalog location gets one.
    *composite_activation*: the served activation's record says it is a
    composite. No grant is read twice."""
    if visibility is None:
        if unrestricted_caller(username, access_service):
            return None
        if not _served_from_catalog(
            served_path, golden_repos_dir, composite_activation
        ):
            return None
        visibility = resolve_meta_visibility(username, access_service)
    if visibility.unrestricted:
        return None
    return CatalogFileView(visibility, Path(served_path), access_service, registry)


def visible_composite_entries(
    listings: List[Any],
    judge_component: Callable[[str], CatalogFileAccess],
) -> List[Any]:
    """The composite entries the caller may see, sorted by ``full_path``.

    Each listing (``component``; ``base``, the directory walked inside it;
    ``entries`` with ``full_path``, ``component_repo``, ``is_directory``) is
    judged once on its own served directory (``judge_component(name)``) and
    each entry on its path inside the component: ``base`` + its path under
    the walked directory.

    Raises:
        ValueError: an entry does not lie under its component (fail closed).
    """
    kept: List[Any] = []
    for listing in listings:
        if not listing.entries:
            continue
        access = judge_component(listing.component)
        prefix = f"{listing.component}/"
        for entry in listing.entries:
            if entry.component_repo != listing.component or not (
                entry.full_path.startswith(prefix)
            ):
                raise ValueError("composite entry outside its component")
            inner = entry.full_path[len(prefix) :]
            relative = f"{listing.base}/{inner}" if listing.base else inner
            if access.lists(relative, entry.is_directory):
                kept.append(entry)
    return sorted(kept, key=lambda entry: entry.full_path)


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
    served = access.served
    if (
        served is not None
        and served != repo_path
        and os.path.realpath(served) != os.path.realpath(repo_path)
    ):
        raise missing
    return access.view


CATALOG_WRITE_REFUSAL = "This path is not available for writing in this repository."
# Decisions whose content is served unchanged: the only catalog paths a
# restricted caller may write.
_WRITABLE = frozenset({DecisionKind.ALLOW, DecisionKind.MEMORY})


def refuse_hidden_catalog_writes(
    repo_path: str, paths: List[str], access: Optional[CatalogFileAccess]
) -> None:
    """Refuse a write into *repo_path* unless the caller sees every one of
    *paths* unchanged (ALLOW or MEMORY), decided on the canonical path
    before any existence check, hash comparison or read: hidden paths,
    descriptions and dependency-map documents are refused alike.
    Unrestricted requests pass unchanged.

    Raises:
        PermissionError: ``CATALOG_WRITE_REFUSAL``, identical whether or not
            the file exists; also for a catalog directory written without a
            judgement or under a judgement made for another directory.
    """
    view = view_for(repo_path, access, PermissionError(CATALOG_WRITE_REFUSAL))
    if view is None:
        return
    if not all(view._served_decision(path).kind in _WRITABLE for path in paths):
        raise PermissionError(CATALOG_WRITE_REFUSAL)


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
