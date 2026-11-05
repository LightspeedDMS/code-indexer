"""The ``wiki`` feature's config section: the keys, their kind and defaults.

Consumed by the core (``config_section()``): every key is a runtime key,
stored in the server's ``feature_settings["wiki"]`` (database, Web UI). Before
this feature owned them they were ``ServerConfig.wiki_config`` (Story #323);
the core moves a stored ``wiki_config`` into the section when it loads a
configuration, so an upgrade loses nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Dict, Mapping

from cidx_sdk import ConfigKey, ConfigSection

#: Section and feature name.
SECTION_NAME = "wiki"

ENABLE_HEADER_BLOCK_PARSING = "enable_header_block_parsing"
ENABLE_ARTICLE_NUMBER = "enable_article_number"
ENABLE_PUBLICATION_STATUS = "enable_publication_status"
ENABLE_VIEWS_SEEDING = "enable_views_seeding"
METADATA_DISPLAY_ORDER = "metadata_display_order"


@dataclass
class WikiConfig:
    """Wiki metadata fields configuration (Story #323).

    Controls which knowledge-base-specific metadata behaviors are active. All
    toggles default to True to preserve current behavior for existing
    deployments. Set individual toggles to False to suppress KB-specific
    fields for generic wikis.
    """

    # Parse and strip legacy header block (Article Number / Title / Publication Status lines)
    enable_header_block_parsing: bool = True
    # Display article_number / original_article in the metadata panel
    enable_article_number: bool = True
    # Display publication_status in the metadata panel
    enable_publication_status: bool = True
    # Seed wiki_article_views from front matter 'views' field; also controls
    # whether the 'views' key appears in the metadata panel as "Salesforce Views"
    enable_views_seeding: bool = True
    # Comma-separated metadata field keys controlling display order in the
    # metadata panel. Empty = current behavior (article_number first, remaining
    # in YAML key order). Non-empty = sort by the configured order, unlisted
    # fields appended alphabetically. Disabled fields excluded regardless.
    metadata_display_order: str = ""


_DESCRIPTIONS = {
    ENABLE_HEADER_BLOCK_PARSING: "Parse and strip the legacy header block "
    "(Article Number / Title / Publication Status lines).",
    ENABLE_ARTICLE_NUMBER: "Show article_number / original_article in the "
    "metadata panel.",
    ENABLE_PUBLICATION_STATUS: "Show publication_status in the metadata panel.",
    ENABLE_VIEWS_SEEDING: "Seed article view counts from the front matter "
    "'views' field and show it in the metadata panel.",
    METADATA_DISPLAY_ORDER: "Comma-separated metadata keys in display order, "
    "e.g. author,created,modified,visibility; unlisted fields follow "
    "alphabetically (empty: default order).",
}

_LABELS = {
    ENABLE_HEADER_BLOCK_PARSING: "Header Block Parsing",
    ENABLE_ARTICLE_NUMBER: "Article Number Field",
    ENABLE_PUBLICATION_STATUS: "Publication Status Field",
    ENABLE_VIEWS_SEEDING: "Views Seeding",
    METADATA_DISPLAY_ORDER: "Metadata Display Order",
}

WIKI_CONFIG_SECTION = ConfigSection(
    name=SECTION_NAME,
    title="Wiki Configuration",
    description=(
        "Which knowledge-base-specific metadata behaviors are active in the "
        "wiki. Every toggle is on by default; turn one off to suppress a "
        "KB-specific field for a generic wiki."
    ),
    keys=tuple(
        ConfigKey(
            name=f.name,
            default=f.default,
            label=_LABELS[f.name],
            description=_DESCRIPTIONS[f.name],
        )
        for f in fields(WikiConfig)
    ),
)


def default_values() -> Dict[str, object]:
    """Every key of the section at its default."""
    return {key.name: key.default for key in WIKI_CONFIG_SECTION.keys}


def wiki_config_from(values: Mapping[str, Any]) -> WikiConfig:
    """A :class:`WikiConfig` from section values (unknown keys ignored,
    missing keys at their defaults)."""
    known = {f.name for f in fields(WikiConfig)}
    return WikiConfig(**{k: v for k, v in values.items() if k in known})
