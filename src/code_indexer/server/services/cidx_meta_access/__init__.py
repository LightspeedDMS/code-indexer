"""Per-caller visibility of the internal catalog repository (cidx-meta).

The one place every cidx-meta door asks "what may this caller see?" and
"what may this caller do with this cidx-meta path?".
"""

from .membership import domain_participants, participants_reader
from .path_policy import Decision, DecisionKind, classify
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
    "UNRESOLVED_IDENTITY",
    "Decision",
    "DecisionKind",
    "MetaVisibility",
    "acting_scope",
    "classify",
    "domain_participants",
    "is_internal_meta_identity",
    "memory_visibility_fn",
    "meta_identity",
    "participants_reader",
    "request_alias_identities",
    "resolve_meta_visibility",
]
