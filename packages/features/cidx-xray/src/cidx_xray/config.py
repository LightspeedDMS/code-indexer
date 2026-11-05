"""The ``xray`` feature's config section: the keys, their kind and defaults.

Consumed by the core (``config_section()``): runtime keys are stored in the
server's ``feature_settings["xray"]`` (database, Web UI), bootstrap keys in
``feature_bootstrap_settings["xray"]`` (``config.json``). Before this feature
owned them they were ``ServerConfig.xray_config`` (runtime) and
``ServerConfig.xray_backend`` (bootstrap); the core moves stored values from
those old keys into the section when it loads a configuration, so an upgrade
loses nothing.
"""

from __future__ import annotations

from typing import Dict

from cidx_sdk import ConfigKey, ConfigSection

#: Section and feature name.
SECTION_NAME = "xray"

TIMEOUT_SECONDS = "xray_timeout_seconds"
WORKER_THREADS = "xray_worker_threads"
BACKEND = "xray_backend"

#: Defaults (also what a process with no feature context reads).
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_WORKER_THREADS = 4
DEFAULT_BACKEND = "local"

XRAY_CONFIG_SECTION = ConfigSection(
    name=SECTION_NAME,
    title="X-Ray Search",
    description=(
        "The X-Ray precision AST-aware code search engine. Changes take "
        "effect immediately for new jobs; no server restart is required."
    ),
    keys=(
        ConfigKey(
            name=TIMEOUT_SECONDS,
            default=DEFAULT_TIMEOUT_SECONDS,
            label="Timeout (seconds)",
            description="Default per-job wall-clock timeout in seconds (10..600).",
            minimum=10,
            maximum=600,
        ),
        ConfigKey(
            name=WORKER_THREADS,
            default=DEFAULT_WORKER_THREADS,
            label="Worker Threads",
            description="Worker threads for Phase 2 AST evaluation (1..8).",
            minimum=1,
            maximum=8,
        ),
        ConfigKey(
            name=BACKEND,
            default=DEFAULT_BACKEND,
            bootstrap=True,
            label="Backend",
            description=(
                "Where caller-supplied evaluators run: 'local' (xray-cli in "
                "this process's host) or 'delegated' (the X-Ray backend "
                "plugin; no fallback to local)."
            ),
        ),
    ),
)


def default_values() -> Dict[str, object]:
    """Every key of the section at its default."""
    return {key.name: key.default for key in XRAY_CONFIG_SECTION.keys}
