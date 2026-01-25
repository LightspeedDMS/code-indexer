#!/usr/bin/env python3
"""
Migration script to add outputSchema from git HEAD version of tools.py to markdown files.

This script reads the TOOL_REGISTRY from the git HEAD version of tools.py and updates
each corresponding .md file with the outputSchema in the YAML frontmatter.

Usage:
    python3 tools/migrate_output_schemas.py [--dry-run]
"""

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


@dataclass
class MigrationStats:
    """Statistics from the migration run."""
    total: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    already_current: int = 0
    no_schema: int = 0


def get_old_tool_registry() -> Dict:
    """Extract TOOL_REGISTRY from git HEAD version of tools.py."""
    result = subprocess.run(
        ['git', 'show', 'HEAD:src/code_indexer/server/mcp/tools.py'],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to get old tools.py from git: {result.stderr}")

    old_content = result.stdout

    # Modify the import statement to avoid dependency on code_indexer
    modified_content = old_content.replace(
        'from code_indexer.server.auth.user_manager import User',
        'User = type("User", (), {"has_permission": lambda self, p: True})'
    )

    exec_globals: Dict = {'__builtins__': __builtins__}
    exec(modified_content, exec_globals)

    return exec_globals.get('TOOL_REGISTRY', {})


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
    field_order = [
        "name", "category", "required_permission", "tl_dr",
        "quick_reference", "parameters", "inputSchema", "outputSchema",
    ]

    ordered = {}
    for key in field_order:
        if key in frontmatter:
            ordered[key] = frontmatter[key]

    for key in frontmatter:
        if key not in ordered:
            ordered[key] = frontmatter[key]

    return yaml.dump(
        ordered, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120
    )


def migrate_output_schema(
    tool_name: str,
    tool_def: dict,
    tool_docs_dir: Path,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """Migrate outputSchema from old TOOL_REGISTRY to markdown file."""
    md_file = find_md_file(tool_name, tool_docs_dir)
    if md_file is None:
        return False, f"No markdown file found for {tool_name}"

    output_schema = tool_def.get("outputSchema")
    if output_schema is None:
        return True, f"No outputSchema in old TOOL_REGISTRY for {tool_name} (skipped)"

    try:
        frontmatter, body = parse_md_file(md_file)
    except ValueError as e:
        return False, str(e)

    if "outputSchema" in frontmatter and frontmatter["outputSchema"] == output_schema:
        return True, f"outputSchema already present and matches for {tool_name}"

    frontmatter["outputSchema"] = output_schema
    new_frontmatter = format_frontmatter(frontmatter)
    new_content = f"---\n{new_frontmatter}---{body}"

    if dry_run:
        return True, f"[DRY-RUN] Would update {md_file.name} with outputSchema"

    md_file.write_text(new_content, encoding="utf-8")
    return True, f"Updated {md_file.name} with outputSchema"


def run_migration(
    tool_registry: Dict,
    tool_docs_dir: Path,
    tools_to_migrate: List[str],
    dry_run: bool
) -> MigrationStats:
    """Run the migration for all specified tools."""
    stats = MigrationStats(total=len(tool_registry))

    for tool_name in tools_to_migrate:
        if tool_name not in tool_registry:
            print(f"ERROR: Tool '{tool_name}' not found in TOOL_REGISTRY")
            stats.failed += 1
            continue

        tool_def = tool_registry[tool_name]
        success, message = migrate_output_schema(tool_name, tool_def, tool_docs_dir, dry_run)

        if success:
            if "already present and matches" in message:
                stats.already_current += 1
                print(f"  OK: {message}")
            elif "No outputSchema" in message:
                stats.no_schema += 1
            else:
                stats.updated += 1
                print(f"  OK: {message}")
        else:
            if "No markdown file found" in message:
                stats.skipped += 1
                print(f"SKIP: {message}")
            else:
                stats.failed += 1
                print(f"FAIL: {message}")

    return stats


def print_summary(stats: MigrationStats, dry_run: bool) -> None:
    """Print the migration summary."""
    print("\n" + "=" * 60)
    print("MIGRATION SUMMARY")
    print("=" * 60)
    print(f"Total tools in registry: {stats.total}")
    print(f"Updated with outputSchema: {stats.updated}")
    print(f"Already current:           {stats.already_current}")
    print(f"No outputSchema (normal):  {stats.no_schema}")
    print(f"Skipped (no md file):      {stats.skipped}")
    print(f"Failed:                    {stats.failed}")

    if dry_run:
        print("\n[DRY-RUN MODE] No files were modified.")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate outputSchema from git HEAD tools.py to markdown files"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    parser.add_argument("--tool", type=str, help="Migrate only a specific tool")
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent
    tool_docs_dir = project_root / "src" / "code_indexer" / "server" / "mcp" / "tool_docs"

    if not tool_docs_dir.exists():
        print(f"ERROR: tool_docs directory not found: {tool_docs_dir}")
        sys.exit(1)

    print("Loading TOOL_REGISTRY from git HEAD...")
    try:
        tool_registry = get_old_tool_registry()
    except Exception as e:
        print(f"ERROR: Failed to load old TOOL_REGISTRY: {e}")
        sys.exit(1)

    print(f"Found {len(tool_registry)} tools in old TOOL_REGISTRY")
    with_output = sum(1 for t in tool_registry.values() if t.get("outputSchema"))
    print(f"Tools with outputSchema: {with_output}")

    tools_to_migrate = [args.tool] if args.tool else list(tool_registry.keys())
    stats = run_migration(tool_registry, tool_docs_dir, tools_to_migrate, args.dry_run)
    print_summary(stats, args.dry_run)


if __name__ == "__main__":
    main()
