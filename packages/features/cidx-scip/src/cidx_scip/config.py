"""The ``scip`` feature's config section: the keys, their kind and defaults.

Consumed by the core (``config_section()``): runtime keys are stored in the
server's ``feature_settings["scip"]`` (database, Web UI), the bootstrap key
in ``feature_bootstrap_settings["scip"]`` (``config.json``). Before this
feature owned them they were ``ServerConfig.scip_config``'s SCIP fields
(runtime) and ``ServerConfig.scip_backend`` (bootstrap); the core moves
stored values from those old keys into the section when it loads a
configuration, so an upgrade loses nothing.
"""

from __future__ import annotations

from typing import Dict

from cidx_sdk import ConfigKey, ConfigSection

from cidx_scip.limits import (
    MAX_SCIP_CALLCHAIN_LIMIT,
    MAX_SCIP_CALLCHAIN_MAX_DEPTH,
    MAX_SCIP_DEPENDENCY_DEPTH,
    MAX_SCIP_REFERENCE_LIMIT,
    MIN_SCIP_CALLCHAIN_LIMIT,
    MIN_SCIP_CALLCHAIN_MAX_DEPTH,
    MIN_SCIP_DEPENDENCY_DEPTH,
    MIN_SCIP_REFERENCE_LIMIT,
)

#: Section and feature name.
SECTION_NAME = "scip"

REFERENCE_LIMIT = "scip_reference_limit"
DEPENDENCY_DEPTH = "scip_dependency_depth"
CALLCHAIN_MAX_DEPTH = "scip_callchain_max_depth"
CALLCHAIN_LIMIT = "scip_callchain_limit"
WORKSPACE_RETENTION_DAYS = "scip_workspace_retention_days"
BACKEND = "scip_backend"

#: Defaults (also what a process with no feature context reads).
DEFAULT_BACKEND = "local"

SCIP_CONFIG_SECTION = ConfigSection(
    name=SECTION_NAME,
    title="SCIP Code Intelligence",
    description=(
        "SCIP (Semantic Code Intelligence Protocol) navigation: definitions, "
        "references, dependencies and call chains. The query limits and the "
        "workspace retention are read once at server startup."
    ),
    keys=(
        ConfigKey(
            name=REFERENCE_LIMIT,
            default=100,
            label="Reference Limit",
            description="Maximum references a SCIP reference query returns (10..10000).",
            restart_required=True,
            minimum=MIN_SCIP_REFERENCE_LIMIT,
            maximum=MAX_SCIP_REFERENCE_LIMIT,
        ),
        ConfigKey(
            name=DEPENDENCY_DEPTH,
            default=3,
            label="Dependency Depth",
            description="Default depth of dependency/dependents queries (1..10).",
            restart_required=True,
            minimum=MIN_SCIP_DEPENDENCY_DEPTH,
            maximum=MAX_SCIP_DEPENDENCY_DEPTH,
        ),
        ConfigKey(
            name=CALLCHAIN_MAX_DEPTH,
            default=3,
            label="Call Chain Max Depth",
            restart_required=True,
            description=(
                "Maximum call chain depth (1..50 accepted; the engine caps "
                "the query at 3, Bug #1603)."
            ),
            minimum=MIN_SCIP_CALLCHAIN_MAX_DEPTH,
            maximum=MAX_SCIP_CALLCHAIN_MAX_DEPTH,
        ),
        ConfigKey(
            name=CALLCHAIN_LIMIT,
            default=100,
            label="Call Chain Limit",
            description="Maximum call chains returned (1..1000).",
            restart_required=True,
            minimum=MIN_SCIP_CALLCHAIN_LIMIT,
            maximum=MAX_SCIP_CALLCHAIN_LIMIT,
        ),
        ConfigKey(
            name=WORKSPACE_RETENTION_DAYS,
            default=7,
            label="Workspace Retention (days)",
            restart_required=True,
            description=(
                "Days a SCIP self-healing workspace is kept before cleanup "
                "(1..365); CIDX_SCIP_WORKSPACE_RETENTION_DAYS overrides it."
            ),
            minimum=1,
            maximum=365,
        ),
        ConfigKey(
            name=BACKEND,
            default=DEFAULT_BACKEND,
            bootstrap=True,
            label="Backend",
            description=(
                "Where SCIP indexes are compiled: 'local' (the per-language "
                "indexers in this pod) or 'delegated-plugin' (the "
                "code_indexer.scip_backend plugin; no fallback to local)."
            ),
        ),
    ),
)


def default_values() -> Dict[str, object]:
    """Every key of the section at its default."""
    return {key.name: key.default for key in SCIP_CONFIG_SECTION.keys}
