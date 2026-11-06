"""The catalog access projection must stay applied at every door that serves it.

The internal catalog (cidx-meta) is filtered per caller: a restricted caller
sees only what the path policy allows, with descriptions and dependency-map
documents projected. That projection is applied by a small family of guards
(``judge_file_door`` / ``judge_app_request`` / ``view_for`` decide; the view's
``lines_view`` / ``served_text`` / ``served_file_payload`` project;
``restricted_catalog_view`` does both for the wiki; ``refuse_hidden_catalog_writes``
guards writes). The wiki is the cidx-wiki feature (modular phase 4e): its
routes and the memory feature's search section (modular phase 4f) reach the
projection only through the core's ``access_projection`` capability, so the
requirements name both ends -- the feature's call sites and the core
capability that binds the projection. Every door that serves
the catalog must call its guard. A door that stops calling it still works, so tests of its normal behaviour do
not notice, and a merge can drop such a call without a conflict marker.

This test reads the door modules with ``ast`` (nothing is imported) and
requires each guard call below, keyed on the enclosing function's qualified
name. The required set is the upstream v12.93.0 wiring.

``catalog_guard_known_gaps.json`` lists requirements this tree does not meet
yet. It works like a ratchet: a listed gap that is now met fails until it is
removed from the file, so a restored guard cannot be deleted again unnoticed.
The file should only ever shrink.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Union

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "src" / "code_indexer"
_KNOWN_GAPS = Path(__file__).with_name("catalog_guard_known_gaps.json")

_FILE_DOORS = "server/services/cidx_meta_access/file_doors.py"
_CATALOG_WIKI = "server/services/cidx_meta_access/catalog_wiki.py"
_ACCESS_PROJECTION = "server/features/access_projection.py"
_FILE_SERVICE = "server/services/file_service.py"
#: Feature modules are named from the repository root ("packages/...").
_WIKI_FEATURE = "packages/features/cidx-wiki/src/cidx_wiki/server"
_WIKI_ROUTES = f"{_WIKI_FEATURE}/routes.py"
_WIKI_CATALOG_VIEW = f"{_WIKI_FEATURE}/catalog_view.py"
_WIKI_ANALYTICS = f"{_WIKI_FEATURE}/mcp/analytics.py"
#: The memory feature's search section (cidx-memory, modular phase 4f).
_MEMORY_RETRIEVAL = "packages/features/cidx-memory/src/cidx_memory/server/retrieval.py"
#: The dependency map's doors (cidx-depmap, modular phase 4g).
_DEPMAP_FEATURE = "packages/features/cidx-depmap/src/cidx_depmap/server"
_DEPMAP_MCP = f"{_DEPMAP_FEATURE}/mcp/handlers.py"
_DEPMAP_CALLER_VIEW = f"{_DEPMAP_FEATURE}/web/caller_view.py"
_DEPMAP_ROUTES = f"{_DEPMAP_FEATURE}/web/routes.py"
_MCP_FILES = "server/mcp/handlers/files.py"
_INLINE_REPOS = "server/routers/inline_repos.py"
_INLINE_REPOS_V2 = "server/routers/inline_repos_v2.py"
_REPOSITORY_HEALTH = "server/routers/repository_health.py"

#: Marker used as the "guard" of a definition requirement.
DEFINED = "<defined>"


@dataclass(frozen=True)
class Requirement:
    module: str  # path under src/code_indexer, or from the repo root (packages/)
    site: str  # qualified name of the function (or the definition itself)
    guard: str  # name that must be called inside ``site``, or DEFINED

    @property
    def key(self) -> str:
        return f"{self.module}::{self.site}::{self.guard}"

    def describe(self) -> str:
        if self.guard == DEFINED:
            return f"guard `{self.site}` is not defined in {self.module}"
        return f"`{self.site}` in {self.module} no longer calls guard `{self.guard}`"


def _calls(module: str, site: str, *guards: str) -> List[Requirement]:
    return [Requirement(module, site, g) for g in guards]


def _defines(module: str, *names: str) -> List[Requirement]:
    return [Requirement(module, n, DEFINED) for n in names]


REQUIRED: List[Requirement] = [
    # The guards themselves.
    *_defines(
        _FILE_DOORS,
        "judge_file_door",
        "judge_app_request",
        "view_for",
        "served_file_payload",
        "refuse_hidden_catalog_writes",
        "CatalogFileView.lines_view",
        "CatalogFileAccess.served_text",
    ),
    *_defines(_CATALOG_WIKI, "restricted_catalog_view"),
    # The core's access_projection capability binds that projection...
    *_calls(
        _ACCESS_PROJECTION,
        "CoreAccessProjectionCapability.catalog_view",
        "restricted_catalog_view",
    ),
    *_calls(
        _ACCESS_PROJECTION,
        "CoreAccessProjectionCapability.visible_article_views",
        "visible_articles",
    ),
    # ...and the wiki feature asks the capability for every caller's view.
    *_calls(_WIKI_CATALOG_VIEW, "restricted_catalog_view", "catalog_view"),
    # File service: listing and content reads take the caller's view.
    *_calls(_FILE_SERVICE, "_catalog_view", "view_for"),
    *_calls(_FILE_SERVICE, "FileListingService.list_files_by_path", "_catalog_view"),
    *_calls(
        _FILE_SERVICE,
        "FileListingService._read_confined",
        "_catalog_view",
        "lines_view",
    ),
    # File writes.
    *_calls(
        "server/services/file_crud_service.py",
        "FileCRUDService._refuse_hidden_catalog_path",
        "refuse_hidden_catalog_writes",
    ),
    # MCP file tools judge the door once, through _judge.
    *_calls(_MCP_FILES, "_judge", "judge_file_door"),
    *[
        Requirement(_MCP_FILES, site, "_judge")
        for site in (
            "browse_directory",
            "get_file_content",
            "list_files",
            "handle_create_file",
            "handle_edit_file",
            "handle_delete_file",
            "_recover_file_content_via_global",
        )
    ],
    # REST file doors.
    *_calls("server/routers/files.py", "_catalog_judge", "judge_app_request"),
    *_calls(
        _INLINE_REPOS, "register_repo_routes.list_repository_files", "judge_app_request"
    ),
    *_calls(
        _INLINE_REPOS_V2,
        "register_repos_v2_routes.list_repository_files",
        "judge_app_request",
        "lines_view",
        "served_file_payload",
    ),
    *_calls(
        "server/managers/composite_file_listing.py",
        "visible_composite_files",
        "judge_app_request",
    ),
    # Repository description (served from the catalog).
    *_calls(
        _REPOSITORY_HEALTH,
        "_read_description",
        "judge_file_door",
        "lines_view",
        "served_text",
    ),
    # Wiki pages, assets and search over the catalog.
    *[
        Requirement(_WIKI_ROUTES, site, "restricted_catalog_view")
        for site in (
            "_user_catalog_view",
            "serve_wiki_root",
            "serve_wiki_article",
            "serve_wiki_asset",
            "wiki_search",
        )
    ],
    *[
        Requirement(_WIKI_ROUTES, site, "_user_catalog_view")
        for site in (
            "serve_user_wiki_root",
            "serve_user_wiki_article",
            "serve_user_wiki_asset",
            "user_wiki_search",
        )
    ],
    # Wiki article analytics over the catalog (MCP wiki_article_analytics).
    *_calls(_WIKI_ANALYTICS, "_visible_views", "visible_article_views"),
    *_calls(_WIKI_ANALYTICS, "handle_wiki_article_analytics", "_visible_views"),
    # Memory files (catalog memories/): the core capability binds the memory
    # scope rule, and the memory feature's search section asks it before any
    # body is read.
    *_calls(
        _ACCESS_PROJECTION,
        "CoreAccessProjectionCapability.visible_memory_ids",
        "filter_visible_memory_ids",
    ),
    *_calls(_MEMORY_RETRIEVAL, "visible_candidates", "visible_memory_ids"),
    *_calls(_MEMORY_RETRIEVAL, "relevant_memories", "visible_candidates"),
    # The dependency map (catalog dependency-map/): the core capability
    # binds the description/dependency-map projection, and every MCP tool
    # and web view of the dependency-map feature asks it for the caller.
    *_calls(
        _ACCESS_PROJECTION,
        "CoreAccessProjectionCapability.dependency_map_access",
        "resolve_meta_visibility",
        "source_for_visibility",
    ),
    *_calls(_DEPMAP_MCP, "_resolve_source", "dependency_map_access"),
    *_calls(_DEPMAP_MCP, "_resolve_parser", "_resolve_source"),
    *[
        Requirement(_DEPMAP_MCP, site, "_resolve_parser")
        for site in (
            "depmap_find_consumers_handler",
            "depmap_get_repo_domains_handler",
            "depmap_get_domain_summary_handler",
            "depmap_get_stale_domains_handler",
            "depmap_get_cross_domain_graph_handler",
        )
    ],
    *_calls(_DEPMAP_MCP, "depmap_get_hub_domains_handler", "_resolve_source"),
    *_calls(_DEPMAP_CALLER_VIEW, "resolve_caller_view", "dependency_map_access"),
    *[
        Requirement(_DEPMAP_ROUTES, site, "resolve_caller_view")
        for site in (
            "depmap_repo_coverage_partial",
            "depmap_domain_explorer_partial",
            "depmap_domain_detail_partial",
            "depmap_graph_data",
        )
    ],
]

_FunctionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef]


def _index_definitions(tree: ast.Module) -> Dict[str, _FunctionNode]:
    """Every function/class in ``tree`` by qualified name (``Outer.inner``)."""
    found: Dict[str, _FunctionNode] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                found.setdefault(name, child)
                visit(child, f"{name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def called_names(node: ast.AST) -> Set[str]:
    """Names called anywhere inside ``node``: ``f(...)`` and ``x.f(...)`` give ``f``."""
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _module_path(src: Path, module: str) -> Path:
    """A feature module is named from the repository root; a core module
    from ``src`` (the checker's own tests pass a synthetic one)."""
    if module.startswith("packages/"):
        return _REPO / module
    return src / module


def unmet(requirements: Iterable[Requirement], src: Path = _SRC) -> Set[str]:
    """Keys of the requirements ``src`` does not meet."""
    parsed: Dict[str, Optional[Dict[str, _FunctionNode]]] = {}
    missing = set()
    for req in requirements:
        if req.module not in parsed:
            path = _module_path(src, req.module)
            parsed[req.module] = (
                _index_definitions(ast.parse(path.read_text(encoding="utf-8")))
                if path.is_file()
                else None
            )
        definitions = parsed[req.module]
        node = definitions.get(req.site) if definitions is not None else None
        if node is None:
            missing.add(req.key)
        elif req.guard != DEFINED and req.guard not in called_names(node):
            missing.add(req.key)
    return missing


def _known_gaps() -> Set[str]:
    data = json.loads(_KNOWN_GAPS.read_text(encoding="utf-8"))
    assert isinstance(data, list), f"{_KNOWN_GAPS.name} must hold a JSON list"
    return set(data)


def test_every_catalog_guard_is_applied_where_required() -> None:
    gaps = _known_gaps()
    by_key = {r.key: r for r in REQUIRED}
    missing = unmet(REQUIRED)
    lost = sorted(missing - gaps)
    assert not lost, (
        "catalog access guard no longer applied where it is required:\n  "
        + "\n  ".join(by_key[k].describe() for k in lost)
    )


def test_known_gaps_only_shrink() -> None:
    gaps = _known_gaps()
    unknown = sorted(gaps - {r.key for r in REQUIRED})
    assert not unknown, f"{_KNOWN_GAPS.name} names no requirement: {unknown}"
    restored = sorted(gaps - unmet(REQUIRED))
    assert not restored, (
        f"guards now in place -- remove these entries from {_KNOWN_GAPS.name} "
        "so they cannot be dropped again unnoticed:\n  " + "\n  ".join(restored)
    )


def test_requirement_keys_are_unique() -> None:
    keys = [r.key for r in REQUIRED]
    assert len(keys) == len(set(keys))


class TestTheChecker:
    """The checker itself, on synthetic sources -- so a broken parser cannot pass."""

    _SOURCE = (
        "def view_for(p):\n"
        "    return p\n"
        "class Service:\n"
        "    def read(self, p):\n"
        "        view = view_for(p)\n"
        "        return view.lines_view(p)\n"
        "    def bare(self, p):\n"
        "        return p\n"
        "def register(app):\n"
        "    @app.get('/x')\n"
        "    async def route(p):\n"
        "        def inner():\n"
        "            return served_text(p)\n"
        "        return inner()\n"
    )

    def _write(self, tmp_path: Path) -> Path:
        (tmp_path / "doors.py").write_text(self._SOURCE, encoding="utf-8")
        return tmp_path

    def test_met_requirements(self, tmp_path: Path) -> None:
        src = self._write(tmp_path)
        reqs = [
            Requirement("doors.py", "view_for", DEFINED),
            Requirement("doors.py", "Service.read", "view_for"),
            Requirement("doors.py", "Service.read", "lines_view"),
            # A guard applied in a nested helper of the site counts.
            Requirement("doors.py", "register.route", "served_text"),
        ]
        assert unmet(reqs, src) == set()

    def test_a_dropped_call_is_reported(self, tmp_path: Path) -> None:
        src = self._write(tmp_path)
        req = Requirement("doors.py", "Service.bare", "view_for")
        assert unmet([req], src) == {req.key}

    def test_a_missing_site_module_or_definition_is_reported(
        self, tmp_path: Path
    ) -> None:
        src = self._write(tmp_path)
        reqs = [
            Requirement("doors.py", "Service.gone", "view_for"),
            Requirement("absent.py", "f", "view_for"),
            Requirement("doors.py", "judge_file_door", DEFINED),
        ]
        assert unmet(reqs, src) == {r.key for r in reqs}

    def test_the_real_tree_is_parsed(self) -> None:
        """Fail loudly, not vacuously, if the door modules move."""
        for module in (_FILE_SERVICE, _WIKI_ROUTES, _MCP_FILES, _INLINE_REPOS):
            path = _module_path(_SRC, module)
            assert path.is_file(), f"{module} moved; update REQUIRED"
