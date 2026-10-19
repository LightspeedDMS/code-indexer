"""Per-caller visibility of the internal catalog repository (cidx-meta).

The one place every cidx-meta door asks "what may this caller see?" and
"what may this caller do with this cidx-meta path?", and the line
projection that turns a catalog document into the caller's view of it.
"""

from .alias_universe import ALIAS_UNIVERSE_TTL_SECONDS, AliasUniverse, UniverseSnapshot
from .depmap_model import DepMapModel, load_depmap_model
from .depmap_projection import (
    DOMAIN_PLACEHOLDER,
    RESTRICTED_PLACEHOLDER,
    MetaProjector,
    ProjectionCacheStats,
    project_chunk,
    project_lines,
    project_meta_document,
    projection_cache_stats,
    render_for_display,
    render_line_aligned,
)
from .membership import domain_participants, participants_reader
from .path_policy import Decision, DecisionKind, classify
from .tree_projection import catalog_tree_paths, restrict_catalog_tree
from .visibility import (
    UNRESOLVED_IDENTITY,
    MetaVisibility,
    acting_scope,
    is_internal_meta_identity,
    memory_visibility_fn,
    meta_identity,
    request_alias_identities,
    resolve_meta_visibility,
)

__all__ = [
    "ALIAS_UNIVERSE_TTL_SECONDS",
    "DOMAIN_PLACEHOLDER",
    "RESTRICTED_PLACEHOLDER",
    "UNRESOLVED_IDENTITY",
    "AliasUniverse",
    "Decision",
    "DecisionKind",
    "DepMapModel",
    "MetaProjector",
    "MetaVisibility",
    "ProjectionCacheStats",
    "UniverseSnapshot",
    "acting_scope",
    "catalog_restriction_applies",
    "catalog_tree_paths",
    "classify",
    "domain_participants",
    "is_catalog_request",
    "is_internal_meta_identity",
    "load_depmap_model",
    "memory_visibility_fn",
    "meta_identity",
    "participants_reader",
    "project_chunk",
    "project_lines",
    "project_meta_document",
    "projection_cache_stats",
    "render_for_display",
    "render_line_aligned",
    "request_alias_identities",
    "resolve_meta_visibility",
    "restrict_catalog_paths",
    "restrict_catalog_tree",
]
