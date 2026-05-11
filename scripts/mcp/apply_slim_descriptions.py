"""
Apply slim_description fields to MCP tool doc frontmatter.

Story #988: MCP Tool Surface Compression - infrastructure script.

Reads a JSON file mapping tool_name -> slim_description text.
For each entry, finds the tool's .md file in tool_docs/, then inserts
(or replaces) the slim_description line immediately after the tl_dr line
using text manipulation - NOT yaml.dump round-trip, to preserve inputSchema
and all other YAML formatting exactly.

File structure: ---\n<frontmatter>\n---\n<body>

Approach:
1. Split on '---' to separate frontmatter and body.
2. Parse frontmatter with yaml.safe_load to find the tl_dr value.
3. In the RAW frontmatter text, find the tl_dr line.
4. Insert slim_description: "<escaped>" on the next line
   (or replace existing slim_description line if present).
5. Reconstruct: ---\n<modified_frontmatter>\n---\n<body>

YAML escaping: always use double quotes; escape internal double quotes
with backslash.

Prints: "Applied N slim_description entries to N tool docs."

Usage:
    PYTHONPATH=./src python3 scripts/mcp/apply_slim_descriptions.py <mapping.json>
"""

import json
import logging
import sys
from pathlib import Path
from typing import Dict, Optional

_REPO_ROOT = Path(__file__).parents[2]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

logger = logging.getLogger(__name__)

_TOOL_DOCS_DIR = _REPO_ROOT / "src" / "code_indexer" / "server" / "mcp" / "tool_docs"


def _escape_yaml_string(value: str) -> str:
    """
    Escape a string value for use as a YAML double-quoted scalar.

    Escapes internal double quotes with backslash.
    The caller wraps the result in double quotes.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_slim_line(value: str) -> str:
    """Return the full slim_description YAML line with double-quoted value."""
    return f'slim_description: "{_escape_yaml_string(value)}"'


def _apply_slim_to_frontmatter(frontmatter_text: str, slim_value: str) -> str:
    """
    Insert or replace slim_description in raw frontmatter text.

    Finds the tl_dr line and inserts slim_description immediately after it.
    If slim_description already exists anywhere in the frontmatter, it is
    replaced (regardless of position) and NOT re-inserted after tl_dr.

    Args:
        frontmatter_text: Raw YAML frontmatter text (without --- delimiters).
        slim_value: The slim_description value to set.

    Returns:
        Modified frontmatter text.
    """
    slim_line = _build_slim_line(slim_value)
    lines = frontmatter_text.split("\n")

    # Check if slim_description already exists
    existing_slim_indices = [
        i for i, line in enumerate(lines) if line.startswith("slim_description:")
    ]

    if existing_slim_indices:
        # Replace existing slim_description line (use first occurrence)
        lines[existing_slim_indices[0]] = slim_line
        # Remove any additional occurrences (defensive)
        for idx in reversed(existing_slim_indices[1:]):
            lines.pop(idx)
        return "\n".join(lines)

    # No existing slim_description: insert after tl_dr line
    tl_dr_index = next(
        (i for i, line in enumerate(lines) if line.startswith("tl_dr:")), None
    )
    if tl_dr_index is None:
        logger.warning("No tl_dr line found in frontmatter; appending slim_description")
        lines.append(slim_line)
    else:
        lines.insert(tl_dr_index + 1, slim_line)

    return "\n".join(lines)


def _find_tool_doc(name: str, tool_docs_root: Path) -> Optional[Path]:
    """
    Find the .md file for a tool by name, walking tool_docs_root.

    Returns the Path if found, or None if no matching file exists.
    """
    for md_file in tool_docs_root.rglob("*.md"):
        if md_file.name.startswith("_"):
            continue
        if md_file.stem == name:
            return md_file
    return None


def apply_slim(
    mapping_path: Path,
    tool_docs_root: Optional[Path] = None,
) -> int:
    """
    Apply slim_description values to tool doc files.

    Reads the JSON mapping, finds each tool's doc, modifies the raw
    frontmatter text to add/replace slim_description, writes back.

    Args:
        mapping_path: Path to JSON file with tool_name -> slim_description.
        tool_docs_root: Directory to search for tool docs.

    Returns:
        Count of successfully applied entries.
    """
    root = tool_docs_root if tool_docs_root is not None else _TOOL_DOCS_DIR
    mapping: Dict[str, str] = json.loads(mapping_path.read_text(encoding="utf-8"))

    applied = 0
    for tool_name, slim_value in mapping.items():
        doc_path = _find_tool_doc(tool_name, root)
        if doc_path is None:
            logger.warning("Tool doc not found for '%s', skipping", tool_name)
            continue

        raw = doc_path.read_text(encoding="utf-8")

        # Split on '---' to get [before_first, frontmatter, body...]
        parts = raw.split("---", 2)
        if len(parts) < 3:
            logger.warning("Unexpected file structure in '%s', skipping", doc_path)
            continue

        before = parts[0]  # typically empty string
        frontmatter = parts[1]  # raw YAML text (without delimiters)
        body = parts[2]  # everything after second ---

        modified_frontmatter = _apply_slim_to_frontmatter(frontmatter, slim_value)

        # Reconstruct preserving exact delimiters
        new_content = before + "---" + modified_frontmatter + "---" + body
        doc_path.write_text(new_content, encoding="utf-8")
        applied += 1

    print(f"Applied {applied} slim_description entries to {applied} tool docs.")
    return applied


def main() -> None:
    """Read mapping from argv[1] and apply slim_descriptions."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: apply_slim_descriptions.py <mapping.json>", file=sys.stderr)
        sys.exit(1)
    apply_slim(mapping_path=Path(sys.argv[1]))


if __name__ == "__main__":
    main()
