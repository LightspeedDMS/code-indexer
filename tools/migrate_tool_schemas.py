#!/usr/bin/env python3
"""
Migration script to add inputSchema from TOOL_REGISTRY to markdown tool docs.

This script reads the current TOOL_REGISTRY from tools.py and updates each
corresponding .md file in tool_docs/ with the inputSchema in the YAML frontmatter.

Usage:
    python3 tools/migrate_tool_schemas.py [--dry-run]

Options:
    --dry-run    Show what would be changed without writing files
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

import yaml


def get_tool_registry():
    """Import and return TOOL_REGISTRY from tools.py."""
    # Add src to path for imports
    src_path = Path(__file__).parent.parent / "src"
    sys.path.insert(0, str(src_path))

    from code_indexer.server.mcp.tools import TOOL_REGISTRY
    return TOOL_REGISTRY


def find_md_file(tool_name: str, tool_docs_dir: Path) -> Optional[Path]:
    """Find the markdown file for a given tool name."""
    for category_dir in tool_docs_dir.iterdir():
        if not category_dir.is_dir():
            continue
        md_file = category_dir / f"{tool_name}.md"
        if md_file.exists():
            return md_file
    return None


def parse_md_file(md_file: Path) -> Tuple[Dict, str]:
    """Parse a markdown file and return (frontmatter_dict, body_content)."""
    content = md_file.read_text(encoding="utf-8")

    if not content.startswith("---"):
        raise ValueError(f"No frontmatter found in {md_file}")

    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"Invalid frontmatter format in {md_file}")

    frontmatter = yaml.safe_load(parts[1])
    if frontmatter is None:
        frontmatter = {}
    body = parts[2]

    return frontmatter, body


def format_frontmatter(frontmatter: dict) -> str:
    """Format frontmatter dict as YAML with proper ordering."""
    # Define field order to maintain consistency
    field_order = [
        "name",
        "category",
        "required_permission",
        "tl_dr",
        "quick_reference",
        "parameters",
        "inputSchema",
    ]

    # Build ordered dict
    ordered = {}
    for key in field_order:
        if key in frontmatter:
            ordered[key] = frontmatter[key]

    # Add any remaining fields not in the order list
    for key in frontmatter:
        if key not in ordered:
            ordered[key] = frontmatter[key]

    # Use custom representer for cleaner output
    return yaml.dump(
        ordered,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )


def migrate_tool_schema(
    tool_name: str,
    tool_def: dict,
    tool_docs_dir: Path,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    Migrate inputSchema from TOOL_REGISTRY to markdown file.

    Returns:
        (success, message) tuple
    """
    # Find the markdown file
    md_file = find_md_file(tool_name, tool_docs_dir)
    if md_file is None:
        return False, f"No markdown file found for {tool_name}"

    # Get inputSchema from registry
    input_schema = tool_def.get("inputSchema")
    if input_schema is None:
        return False, f"No inputSchema in TOOL_REGISTRY for {tool_name}"

    # Parse existing markdown file
    try:
        frontmatter, body = parse_md_file(md_file)
    except ValueError as e:
        return False, str(e)

    # Check if inputSchema already exists
    if "inputSchema" in frontmatter:
        # Compare to see if update needed
        if frontmatter["inputSchema"] == input_schema:
            return True, f"inputSchema already present and matches for {tool_name}"

    # Add/update inputSchema
    frontmatter["inputSchema"] = input_schema

    # Format new content
    new_frontmatter = format_frontmatter(frontmatter)
    new_content = f"---\n{new_frontmatter}---{body}"

    if dry_run:
        return True, f"[DRY-RUN] Would update {md_file.name} with inputSchema"

    # Write updated file
    md_file.write_text(new_content, encoding="utf-8")
    return True, f"Updated {md_file.name} with inputSchema"


def main():
    parser = argparse.ArgumentParser(
        description="Migrate inputSchema from TOOL_REGISTRY to markdown tool docs"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without writing files",
    )
    parser.add_argument(
        "--tool",
        type=str,
        help="Migrate only a specific tool (by name)",
    )
    args = parser.parse_args()

    # Get paths
    project_root = Path(__file__).parent.parent
    tool_docs_dir = project_root / "src" / "code_indexer" / "server" / "mcp" / "tool_docs"

    if not tool_docs_dir.exists():
        print(f"ERROR: tool_docs directory not found: {tool_docs_dir}")
        sys.exit(1)

    # Get TOOL_REGISTRY
    print("Loading TOOL_REGISTRY...")
    tool_registry = get_tool_registry()
    print(f"Found {len(tool_registry)} tools in TOOL_REGISTRY")

    # Track statistics
    updated = 0
    skipped = 0
    failed = 0
    already_current = 0

    # Migrate each tool
    tools_to_migrate = [args.tool] if args.tool else tool_registry.keys()

    for tool_name in tools_to_migrate:
        if tool_name not in tool_registry:
            print(f"ERROR: Tool '{tool_name}' not found in TOOL_REGISTRY")
            failed += 1
            continue

        tool_def = tool_registry[tool_name]
        success, message = migrate_tool_schema(
            tool_name, tool_def, tool_docs_dir, dry_run=args.dry_run
        )

        if success:
            if "already present and matches" in message:
                already_current += 1
            else:
                updated += 1
            print(f"  OK: {message}")
        else:
            if "No markdown file found" in message:
                skipped += 1
                print(f"SKIP: {message}")
            else:
                failed += 1
                print(f"FAIL: {message}")

    # Summary
    print("\n" + "=" * 60)
    print("MIGRATION SUMMARY")
    print("=" * 60)
    print(f"Total tools in registry: {len(tool_registry)}")
    print(f"Updated:                 {updated}")
    print(f"Already current:         {already_current}")
    print(f"Skipped (no md file):    {skipped}")
    print(f"Failed:                  {failed}")

    if args.dry_run:
        print("\n[DRY-RUN MODE] No files were modified.")


if __name__ == "__main__":
    main()
