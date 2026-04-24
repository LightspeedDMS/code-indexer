"""
dep_map_parser_tables -- low-level markdown table and frontmatter helpers.

Extracted from dep_map_mcp_parser.py (Story #887 AC8 module split).
All functions are pure or read-only with respect to the filesystem.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml

# Any is justified: PyYAML safe_load returns heterogeneous Python primitives
# (str, int, float, bool, list, dict, None) -- no narrower structural type exists.

from code_indexer.server.services.dep_map_parser_hygiene import strip_backticks

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column indices -- shared constants
# ---------------------------------------------------------------------------

# Incoming Dependencies table (0-based, after stripping outer pipes)
_COL_EXTERNAL_REPO = 0
_COL_DEPENDS_ON = 1
_COL_SOURCE_DOMAIN = 2
_COL_DEP_TYPE = 3
_COL_WHY = 4
_COL_EVIDENCE = 5
_INCOMING_MIN_COLS = 6

# Repository Roles table (0-based)
_COL_ROLES_REPO = 0
_COL_ROLES_ROLE = 2
_ROLES_MIN_COLS = 3
_ROLES_HEADER_SENTINEL = "Repository"

# Outgoing Dependencies table (0-based)
_COL_OUTGOING_TARGET_DOMAIN = 2
_OUTGOING_MIN_COLS = 4
_OUTGOING_HEADER_SENTINEL = "This Repo"

# Incoming header sentinel
_INCOMING_HEADER_SENTINEL = "External Repo"


# ---------------------------------------------------------------------------
# Shared section-scan primitive
# ---------------------------------------------------------------------------


def _scan_section_lines(
    content: str,
    section_heading: str,
) -> Iterator[Tuple[bool, str]]:
    """Yield (in_section, stripped_line) for every line in *content*.

    Activates when the exact ``section_heading`` is encountered (the heading
    line itself is NOT yielded). Deactivates and stops when a heading of the
    same or higher level follows.

    Raises:
        ValueError: when content is not a str or section_heading is empty.
    """
    if not isinstance(content, str):
        raise ValueError("content must be a str")
    if not isinstance(section_heading, str) or not section_heading.strip():
        raise ValueError("section_heading must be a non-empty str")

    section_level = len(section_heading) - len(section_heading.lstrip("#"))
    in_section = False

    for line in content.splitlines():
        stripped = line.strip()

        if stripped == section_heading:
            in_section = True
            continue

        if in_section and stripped.startswith("#"):
            lvl = len(stripped) - len(stripped.lstrip("#"))
            if lvl > 0 and stripped[lvl : lvl + 1] == " " and lvl <= section_level:
                return

        yield in_section, stripped


# ---------------------------------------------------------------------------
# Public table helpers
# ---------------------------------------------------------------------------


def iter_table_rows(
    content: str,
    section_heading: str,
    min_cols: int,
    header_sentinel: str,
) -> Iterator[List[str]]:
    """Yield cell lists for each data row in a named markdown table section.

    Raises:
        ValueError: when any argument fails validation.
    """
    if not isinstance(content, str):
        raise ValueError("content must be a str")
    if not isinstance(section_heading, str) or not section_heading.strip():
        raise ValueError("section_heading must be a non-empty str")
    if isinstance(min_cols, bool) or not isinstance(min_cols, int) or min_cols < 1:
        raise ValueError("min_cols must be a positive integer")
    if not isinstance(header_sentinel, str):
        raise ValueError("header_sentinel must be a str")

    for in_section, stripped in _scan_section_lines(content, section_heading):
        if not in_section:
            continue
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [c.strip() for c in stripped.split("|")[1:-1]]
        if len(cells) < min_cols:
            continue
        if cells[0] == header_sentinel:
            continue
        if set(cells[0]) <= frozenset("-"):
            continue
        yield cells


def validate_section_has_table(content: str, section_heading: str) -> None:
    """Raise ValueError when a section heading is present but has no table rows.

    Absent sections are a no-op (sections are optional).

    Raises:
        ValueError: content not a str; section_heading empty; or heading found
                    but no pipe-delimited rows follow before the next same/higher
                    level heading.
    """
    if not isinstance(content, str):
        raise ValueError("content must be a str")
    if not isinstance(section_heading, str) or not section_heading.strip():
        raise ValueError("section_heading must be a non-empty str")

    section_present = section_heading in content.splitlines()
    if not section_present:
        return

    found_table_row = any(
        stripped.startswith("|") and stripped.endswith("|")
        for in_section, stripped in _scan_section_lines(content, section_heading)
        if in_section
    )

    if not found_table_row:
        raise ValueError(
            f"section '{section_heading}' is present but contains no table rows"
        )


def parse_roles_table(content: str) -> Dict[str, str]:
    """Extract repo->role mapping from the '## Repository Roles' table."""
    result: Dict[str, str] = {}
    for cells in iter_table_rows(
        content, "## Repository Roles", _ROLES_MIN_COLS, _ROLES_HEADER_SENTINEL
    ):
        repo = strip_backticks(
            cells[_COL_ROLES_REPO].strip("*")
        )  # strip bold + backticks (AC1)
        role = cells[_COL_ROLES_ROLE]
        if repo:
            result[repo] = role
    return result


def parse_outgoing_table(content: str) -> Dict[str, int]:
    """Count rows per target_domain in the '### Outgoing Dependencies' table."""
    counts: Dict[str, int] = defaultdict(int)
    for cells in iter_table_rows(
        content,
        "### Outgoing Dependencies",
        _OUTGOING_MIN_COLS,
        _OUTGOING_HEADER_SENTINEL,
    ):
        target = cells[_COL_OUTGOING_TARGET_DOMAIN]
        if target:
            counts[target] += 1
    return dict(counts)


def parse_incoming_table(content: str) -> List[Dict[str, str]]:
    """Extract rows from the '### Incoming Dependencies' table."""
    rows: List[Dict[str, str]] = []
    for cells in iter_table_rows(
        content,
        "### Incoming Dependencies",
        _INCOMING_MIN_COLS,
        _INCOMING_HEADER_SENTINEL,
    ):
        rows.append(
            {
                "external_repo": cells[_COL_EXTERNAL_REPO],
                "depends_on": cells[_COL_DEPENDS_ON],
                "source_domain": cells[_COL_SOURCE_DOMAIN],
                "dep_type": cells[_COL_DEP_TYPE],
                "why": cells[_COL_WHY],
                "evidence": cells[_COL_EVIDENCE],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Frontmatter and summary helpers
# ---------------------------------------------------------------------------


def parse_frontmatter_strict(content: str) -> Optional[Dict[str, Any]]:
    """Parse YAML frontmatter, raising on malformed YAML or non-dict result.

    Unlike dep_map_file_utils.parse_yaml_frontmatter (which silently returns
    None on errors), this raises so that the caller can record an anomaly.

    Returns None when content has no '---' opener or frontmatter block is absent.

    Raises:
        ValueError: content not a str; or parsed result is not a dict.
        yaml.YAMLError: when the frontmatter block is malformed YAML.
    """
    if not isinstance(content, str):
        raise ValueError("content must be a str")
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError("frontmatter block opened with '---' but never closed")
    result = yaml.safe_load(parts[1])
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ValueError(
            f"frontmatter must be a YAML mapping, got {type(result).__name__}"
        )
    return result


def parse_last_analyzed(raw: str) -> datetime:
    """Parse a last_analyzed ISO-8601 string to a UTC-normalized datetime.

    Raises:
        ValueError: raw is not a str; string cannot be parsed; or is timezone-naive.
    """
    if not isinstance(raw, str):
        raise ValueError("raw must be a str")
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            "last_analyzed must be timezone-aware (missing Z or offset); "
            f"got naive value {raw!r}"
        )
    return dt.astimezone(timezone.utc)


def build_name_description(
    content: str,
    md_file: Path,
    fallback_name: str,
    fallback_description: str = "",
) -> Tuple[str, str, Optional[Dict[str, str]]]:
    """Parse name and description from YAML frontmatter in the .md file.

    Delegates entirely to parse_frontmatter_strict() -- no duplicate yaml parsing.

    Returns:
        (name, description, None) on successful parse.
        ("", "", anomaly_dict) on frontmatter parse error, after logging.
        (fallback_name, fallback_description, None) when no frontmatter present.
    """
    if not content:
        return fallback_name, fallback_description, None
    try:
        fm = parse_frontmatter_strict(content)
        if fm is None:
            return fallback_name, fallback_description, None
        name = fm["name"] if "name" in fm else fallback_name
        description = fm["description"] if "description" in fm else fallback_description
        return name, description, None
    except (yaml.YAMLError, ValueError) as exc:
        logger.warning(
            "get_domain_summary: failed to parse frontmatter in %s: %s",
            md_file,
            exc,
        )
        return "", "", {"file": str(md_file), "error": f"frontmatter: {exc}"}


def build_participating_repos(
    content: str,
    md_file: Path,
) -> Tuple[List[Dict[str, str]], Optional[Dict[str, str]]]:
    """Extract participating_repos from the Repository Roles table.

    Returns:
        ([{repo, role}, ...], None) on success.
        ([], anomaly_dict) on parse error, after logging a warning.
        ([], None) when content is empty (file was not readable).
    """
    if not content:
        return [], None
    try:
        validate_section_has_table(content, "## Repository Roles")
        roles_map = parse_roles_table(content)
        return [{"repo": r, "role": role} for r, role in roles_map.items()], None
    except (yaml.YAMLError, ValueError) as exc:
        logger.warning(
            "get_domain_summary: failed to parse roles table in %s: %s",
            md_file,
            exc,
        )
        return [], {"file": str(md_file), "error": f"participating_repos: {exc}"}


def build_cross_domain_connections(
    content: str,
    md_file: Path,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, str]]]:
    """Extract cross_domain_connections from the Outgoing Dependencies table.

    Returns:
        ([{target_domain, dependency_count}, ...], None) on success.
        ([], anomaly_dict) on parse error, after logging a warning.
        ([], None) when content is empty (file was not readable).
    """
    if not content:
        return [], None
    try:
        validate_section_has_table(content, "### Outgoing Dependencies")
        counts = parse_outgoing_table(content)
        return (
            [{"target_domain": t, "dependency_count": c} for t, c in counts.items()],
            None,
        )
    except (yaml.YAMLError, ValueError) as exc:
        logger.warning(
            "get_domain_summary: failed to parse outgoing table in %s: %s",
            md_file,
            exc,
        )
        return [], {"file": str(md_file), "error": f"cross_domain_connections: {exc}"}
