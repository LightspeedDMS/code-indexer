"""The ``depmap`` feature's config section: the keys, their kind and defaults.

Consumed by the core (``config_section()``): every key is a runtime key,
stored in the server's ``feature_settings["depmap"]`` (database, Web UI: the
Feature Settings block of the config screen).
Before this feature owned them they were fields of
``ServerConfig.claude_integration_config``; the core moves the stored values
into the section when it loads a configuration, so an upgrade loses nothing.

The feature reads them through :class:`DepMapClaudeConfig`, a view of the
core's ``claude_integration_config`` that answers these keys from the section
(``cidx_depmap.server.runtime``).
"""

from __future__ import annotations

from typing import Dict, Tuple

from cidx_sdk import ConfigKey, ConfigSection

#: Section and feature name.
SECTION_NAME = "depmap"

#: name -> (label, default, description, minimum, maximum)
_KEYS: Dict[str, Tuple[str, object, str, object, object]] = {
    "dependency_map_enabled": (
        "Dependency Map Enabled",
        False,
        "Run the scheduled cross-repository dependency-map analysis.",
        None,
        None,
    ),
    "dependency_map_interval_hours": (
        "Refresh Interval (hours)",
        168,
        "Hours between scheduled delta analyses (default 168 = 1 week).",
        1,
        None,
    ),
    "dependency_map_pass_timeout_seconds": (
        "Pass Timeout (seconds)",
        1800,
        "Analysis pass timeout (Pass 2 uses all of it, Pass 1/3 half).",
        60,
        None,
    ),
    "dep_map_fact_check_enabled": (
        "Post-generation verification pass (fact-check)",
        False,
        "Fact-check generated documents (dependency map and repository "
        "descriptions) against the source. Adds latency.",
        None,
        None,
    ),
    "fact_check_timeout_seconds": (
        "Verification timeout (seconds)",
        600,
        "Per-invocation timeout of the verification call (60..3600); retried "
        "once after 30s, the original content is kept on a second timeout.",
        60,
        3600,
    ),
    "dependency_map_pass1_max_turns": (
        "Pass 1 Max Turns (Synthesis)",
        0,
        "Pass 1 (domain clustering) turn limit; 0 = single shot.",
        0,
        None,
    ),
    "dependency_map_pass2_max_turns": (
        "Pass 2 Max Turns (Per-Domain)",
        0,
        "Pass 2 (per domain) turn limit; 0 = unlimited.",
        0,
        None,
    ),
    "dependency_map_delta_max_turns": (
        "Delta Max Turns",
        0,
        "Delta analysis turn limit; 0 = unlimited.",
        0,
        None,
    ),
    "refinement_enabled": (
        "Refinement Enabled",
        False,
        "Run the scheduled refinement (fact-check) of dependency documents.",
        None,
        None,
    ),
    "refinement_interval_hours": (
        "Refinement Interval (hours)",
        24,
        "Hours between refinement runs.",
        1,
        None,
    ),
    "refinement_domains_per_run": (
        "Domains Per Run",
        3,
        "Domains refined per run (1..50).",
        1,
        50,
    ),
    "dep_map_auto_repair_enabled": (
        "Auto-repair after scheduled jobs",
        False,
        "Repair once after a scheduled delta or refinement job that left "
        "anomalies; anomalies left are retried on the next cycle.",
        None,
        None,
    ),
}

#: Keys read once at server startup (the scheduler is started or not).
_RESTART_REQUIRED = frozenset({"dependency_map_enabled"})

#: The keys this feature owns (formerly claude_integration_config fields).
OWNED_KEYS = frozenset(_KEYS)

DEPMAP_CONFIG_SECTION = ConfigSection(
    name=SECTION_NAME,
    title="Dependency Map",
    description=(
        "Cross-repository dependency-map generation, document refinement, "
        "post-generation verification and scheduled auto-repair. The Claude "
        "model is the core's Analysis Model (Golden Repository Settings)."
    ),
    keys=tuple(
        ConfigKey(
            name=name,
            default=default,
            label=label,
            description=description,
            minimum=minimum if isinstance(minimum, (int, float)) else None,
            maximum=maximum if isinstance(maximum, (int, float)) else None,
            restart_required=name in _RESTART_REQUIRED,
        )
        for name, (label, default, description, minimum, maximum) in _KEYS.items()
    ),
)


def default_values() -> Dict[str, object]:
    """Every key of the section at its default."""
    return {key.name: key.default for key in DEPMAP_CONFIG_SECTION.keys}
