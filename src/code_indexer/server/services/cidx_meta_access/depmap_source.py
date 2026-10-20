"""The caller's dependency map as a ``DepMapSource``.

Administrators read the directory as it is (``FilesystemDepMapSource``).
Everyone else gets ``ProjectedDepMapSource``: only documents the path
policy lets them see, each projected and rendered for display (removed
lines dropped, so the table parsers see well-formed tables), and
``_domains.json`` entries reduced to visible domains and visible
participants; a description is projected only when a door asks for it
(``describe``). Labels are catalog-relative (``dependency-map/<name>.md``),
never an absolute server path.

Built per request. Classification and the ``_domains.json`` entries come
from the dependency-map model the projection loads (one source of truth,
cached per directory fingerprint); projected documents come from the
projection caches. Synchronous file I/O: async callers use a worker thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..dep_map_source import DepMapSource, FilesystemDepMapSource
from ..repo_access_guard import normalize_repo_alias
from .depmap_projection import MetaProjector, render_for_display
from .membership import model_participants_reader
from .path_policy import DEP_MAP_DIR, Decision, DecisionKind, classify
from .universe_provider import process_alias_universe
from .visibility import MetaVisibility, resolve_meta_visibility

# A domain document is only ever TRANSFORM (visible) or DENY; ALLOW covers
# the underscore support documents (_dep_types, _analysis_guidelines).
_READABLE = frozenset({DecisionKind.TRANSFORM, DecisionKind.ALLOW})
_DOMAINS_JSON = f"{DEP_MAP_DIR}/_domains.json"


def _no_memory(_memory_id: str) -> bool:
    return False


class ProjectedDepMapSource:
    """What one restricted caller may see of a dependency-map directory.

    One object per request, used by that request's thread only (its memo
    dicts are never shared)."""

    def __init__(
        self, output_dir: Path, vis: MetaVisibility, projector: MetaProjector
    ) -> None:
        if vis.unrestricted:
            raise ValueError("an unrestricted caller reads the filesystem source")
        self._files = FilesystemDepMapSource(output_dir)
        self._vis = vis
        self._projector = projector
        self._model = projector.model()
        self._participants = model_participants_reader(Path(output_dir), self._model)
        self._decisions: Dict[str, Decision] = {}
        self._descriptions: Dict[str, str] = {}

    def _decide(self, name: str) -> Decision:
        decision = self._decisions.get(name)
        if decision is None:
            decision = classify(
                f"{DEP_MAP_DIR}/{name}.md", self._vis, self._participants, _no_memory
            )
            self._decisions[name] = decision
        return decision

    def _visible_domain(self, name: str) -> bool:
        decision = self._decide(name)
        return decision.kind is DecisionKind.TRANSFORM and decision.subject == name

    def _redact(self, text: str, relative_path: str) -> str:
        projected = self._projector.project(text.splitlines(), relative_path)
        return render_for_display(projected)

    def is_present(self) -> bool:
        return self._files.is_present()

    def load_domains_json(self) -> List[Dict[str, Any]]:
        visible: List[Dict[str, Any]] = []
        for entry in self._model.domain_entries:
            name = entry.get("name") if isinstance(entry, Mapping) else None
            if not isinstance(name, str) or not self._visible_domain(name):
                continue
            description = entry.get("description", "")
            self._descriptions[name] = (
                description if isinstance(description, str) else ""
            )
            visible.append(
                {
                    "name": name,
                    "participating_repos": self._visible_participants(name, entry),
                }
            )
        return visible

    def describe(self, entry: Mapping[str, Any]) -> str:
        """The projected description of an entry this source returned; ""
        for any other."""
        raw = self._descriptions.get(str(entry.get("name", "")), "")
        return self._redact(raw, _DOMAINS_JSON) if raw else ""

    def _visible_participants(self, name: str, entry: Mapping[str, Any]) -> List[str]:
        """The entry's visible repositories; an entry naming none lists the
        visible repositories of the domain's derived membership."""
        repos = entry.get("participating_repos")
        named = (
            [r for r in repos if isinstance(r, str)] if isinstance(repos, list) else []
        )
        if named:
            return [r for r in named if normalize_repo_alias(r) in self._vis.visible]
        lowered = {p.lower() for p in self._participants(name) or ()}
        return sorted(v for v in self._vis.visible if v.lower() in lowered)

    def domain_names(self) -> List[str]:
        return [n for n in self._files.domain_names() if self._visible_domain(n)]

    def has_domain(self, name: str) -> bool:
        return self._decide(name).kind in _READABLE and self._files.has_domain(name)

    def read_text(self, name: str) -> str:
        decision = self._decide(name)
        if decision.kind not in _READABLE:
            raise FileNotFoundError(self.label(name))
        content = self._files.read_text(name)
        if decision.kind is DecisionKind.ALLOW:
            return content
        return self._redact(content, f"{DEP_MAP_DIR}/{name}.md")

    def label(self, name: str) -> str:
        return f"{DEP_MAP_DIR}/{name}.md"

    def resolved_label(self, name: str) -> str:
        return self.label(name)

    def within(self, name: str) -> bool:
        return self._decide(name).kind in _READABLE


def resolve_depmap_source(
    username: str,
    access_service: Optional[Any],
    output_dir: Path,
    registry: Any,
) -> Tuple[DepMapSource, bool]:
    """The caller's source of *output_dir* and whether it is restricted.
    Visibility is resolved fresh (acting-user scope honoured)."""
    vis = resolve_meta_visibility(username, access_service)
    return source_for_visibility(vis, output_dir, registry), not vis.unrestricted


def source_for_visibility(
    vis: MetaVisibility, output_dir: Path, registry: Any
) -> DepMapSource:
    """The source of *output_dir* for an already-resolved visibility."""
    if vis.unrestricted:
        return FilesystemDepMapSource(output_dir)
    projector = MetaProjector(
        vis, dep_map_dir=output_dir, universe=process_alias_universe(registry)
    )
    return ProjectedDepMapSource(output_dir, vis, projector)
