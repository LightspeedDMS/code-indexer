"""
Shared file utilities for dependency map services (Story #342).

Provides common operations used by DepMapHealthDetector, IndexRegenerator,
and DepMapRepairExecutor to avoid code duplication.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def load_domains_json(output_dir: Path) -> List[Dict[str, Any]]:
    """Load _domains.json, returning empty list if missing or invalid."""
    domains_file = output_dir / "_domains.json"
    if not domains_file.exists():
        return []
    try:
        data = json.loads(domains_file.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        logger.warning("_domains.json is not a list: %s", type(data))
        return []
    except Exception as e:
        logger.warning("Failed to load _domains.json: %s", e)
        return []


def has_yaml_frontmatter(content: str) -> bool:
    """
    Check if content starts with a YAML frontmatter block (--- ... ---).

    Returns True if the content begins with '---' and has a closing '---'.
    """
    if not content.startswith("---"):
        return False
    rest = content[3:]
    return "\n---" in rest


def parse_simple_yaml(lines: List[str]) -> Dict[str, Any]:
    """
    Parse a simplified YAML structure from frontmatter lines.

    Handles:
      key: scalar_value
      key:
        - list_item
    """
    result: Dict[str, Any] = {}
    current_key: Optional[str] = None
    current_list: Optional[List[str]] = None

    for line in lines:
        if not line.strip():
            continue

        # List item
        if line.startswith("  - ") or line.startswith("- "):
            if current_key is not None:
                item = line.strip().lstrip("- ").strip()
                if current_list is None:
                    current_list = []
                    result[current_key] = current_list
                current_list.append(item)
            continue

        # Key: value or Key:
        if ":" in line:
            # Save previous list if any
            current_list = None

            colon_idx = line.index(":")
            key = line[:colon_idx].strip()
            value_part = line[colon_idx + 1:].strip()

            current_key = key
            if value_part:
                # Strip surrounding quotes if present
                if (value_part.startswith('"') and value_part.endswith('"')) or (
                    value_part.startswith("'") and value_part.endswith("'")
                ):
                    value_part = value_part[1:-1]
                result[key] = value_part
            # else: value is on subsequent lines (list items)

    return result


def parse_yaml_frontmatter(content: str) -> Optional[Dict[str, Any]]:
    """
    Parse YAML frontmatter block from markdown content.

    Returns dict of parsed values, or None if no frontmatter present.
    Handles simple key: value and key: [list] patterns without a full YAML parser.
    """
    if not has_yaml_frontmatter(content):
        return None

    # Extract frontmatter block between --- delimiters
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return None

    fm_lines = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        fm_lines.append(line)

    return parse_simple_yaml(fm_lines)


def get_domain_md_files(output_dir: Path) -> List[Path]:
    """
    Return list of .md files in output_dir that are NOT underscore-prefixed.

    Excludes _index.md, _domains.json, _activity.md, and any other _*.md files.
    """
    return [
        f
        for f in output_dir.glob("*.md")
        if not f.name.startswith("_")
    ]
