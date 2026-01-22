#!/usr/bin/env python3
"""
Convert TOOL_REGISTRY descriptions to external markdown files.

Story #14: Externalize MCP Tool Documentation to Markdown Files
AC3: Conversion Script Output - Generate 128 .md files with valid frontmatter.

Usage:
    python3 tools/convert_tool_docs.py [--output-dir PATH]
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Any

import yaml


MAX_TLDR_LENGTH = 100

# Tool name patterns for category assignment
CATEGORY_PATTERNS = [
    (
        r"^search_code$|^regex_search$|^browse_directory$|^directory_tree$|^get_file_content$|^list_files$|^get_cached_content$",
        "search",
    ),
    (r"^git_", "git"),
    (r"^scip_", "scip"),
    (r"^create_file$|^edit_file$|^delete_file$", "files"),
    (r"^cidx_ssh_key_", "ssh"),
    (r"^gh_actions_|^github_actions_|^gitlab_ci_", "cicd"),
    (r"^first_time_user_guide$|^get_tool_categories$|^cidx_quick_reference$", "guides"),
    (
        r"^activate_repository$|^deactivate_repository$|^sync_repository$|^switch_branch$|^list_repositories$|^get_repository_status$|^get_repository_statistics$|^get_all_repositories_status$|^manage_composite_repository$|^get_branches$|^discover_repositories$",
        "repos",
    ),
    (
        r"^add_golden_repo$|^remove_golden_repo$|^refresh_golden_repo$|^list_global_repos$|^global_repo_status$|^get_golden_repo_indexes$|^add_golden_repo_index$|^get_global_config$|^set_global_config$|^trigger_reindex$|^get_index_status$",
        "repos",
    ),
    # Admin tools - user management, groups, credentials, API keys, maintenance, logs
    (r"^create_user$|^list_users$|^delete_user$", "admin"),
    (
        r"^create_group$|^delete_group$|^get_group$|^list_groups$|^update_group$|^add_member_to_group$|^remove_member_from_group$|^add_repos_to_group$|^remove_repo_from_group$|^bulk_remove_repos_from_group$",
        "admin",
    ),
    (r"^create_api_key$|^delete_api_key$|^list_api_keys$", "admin"),
    (
        r"^create_mcp_credential$|^delete_mcp_credential$|^list_mcp_credentials$",
        "admin",
    ),
    (
        r"^admin_create_user_mcp_credential$|^admin_delete_user_mcp_credential$|^admin_list_all_mcp_credentials$|^admin_list_user_mcp_credentials$",
        "admin",
    ),
    (r"^admin_logs_export$|^admin_logs_query$|^query_audit_logs$", "admin"),
    (
        r"^enter_maintenance_mode$|^exit_maintenance_mode$|^get_maintenance_status$",
        "admin",
    ),
    (
        r"^authenticate$|^check_health$|^get_job_details$|^get_job_statistics$|^set_session_impersonation$",
        "admin",
    ),
    (
        r"^execute_delegation_function$|^list_delegation_functions$|^poll_delegation_job$",
        "admin",
    ),
]


def categorize_tool(tool_name: str) -> str:
    """Determine the category for a tool based on its name."""
    for pattern, category in CATEGORY_PATTERNS:
        if re.match(pattern, tool_name):
            return category
    return "admin"


def extract_tl_dr(description: str) -> str:
    """Extract TL;DR summary from description."""
    match = re.search(r"TL;DR:\s*([^.]+\.)", description)
    if match:
        return match.group(1).strip()

    sentences = description.split(". ")
    if sentences:
        return sentences[0].strip().rstrip(".") + "."

    return description[:MAX_TLDR_LENGTH]


def convert_tool(
    tool_name: str, tool_def: Dict[str, Any], output_dir: Path, category: str
) -> bool:
    """Convert a single tool definition to a markdown file."""
    description = tool_def.get("description", "")
    permission = tool_def.get("required_permission", "")
    tl_dr = extract_tl_dr(description)

    frontmatter_dict = {
        "name": tool_name,
        "category": category,
        "required_permission": permission,
        "tl_dr": tl_dr,
    }
    frontmatter_yaml = yaml.dump(
        frontmatter_dict, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    content = f"---\n{frontmatter_yaml}---\n\n{description}"

    md_file = output_dir / category / f"{tool_name}.md"
    md_file.parent.mkdir(parents=True, exist_ok=True)
    md_file.write_text(content, encoding="utf-8")
    return True


def convert_all_tools(
    tool_registry: Dict[str, Any], output_dir: Path
) -> Dict[str, int]:
    """Convert all tools in registry to markdown files."""
    stats = {"total": 0, "converted": 0, "failed": 0, "by_category": {}}

    for tool_name, tool_def in tool_registry.items():
        stats["total"] += 1
        category = categorize_tool(tool_name)

        if category not in stats["by_category"]:
            stats["by_category"][category] = 0

        try:
            convert_tool(tool_name, tool_def, output_dir, category)
            stats["converted"] += 1
            stats["by_category"][category] += 1
        except Exception as e:
            stats["failed"] += 1
            print(f"ERROR: Failed to convert {tool_name}: {e}")

    return stats


def main():
    """Main entry point for CLI usage."""
    parser = argparse.ArgumentParser(
        description="Convert TOOL_REGISTRY to markdown files"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("src/code_indexer/server/mcp/tool_docs"),
        help="Output directory for .md files",
    )
    args = parser.parse_args()

    if not args.output_dir.exists():
        print(f"ERROR: Output directory does not exist: {args.output_dir}")
        sys.exit(1)

    sys.path.insert(0, "src")
    from code_indexer.server.mcp.tools import TOOL_REGISTRY

    print(f"Converting {len(TOOL_REGISTRY)} tools...")
    stats = convert_all_tools(TOOL_REGISTRY, args.output_dir)

    print("\nConversion complete:")
    print(f"  Total: {stats['total']}")
    print(f"  Converted: {stats['converted']}")
    print(f"  Failed: {stats['failed']}")
    print("\nBy category:")
    for cat, count in sorted(stats["by_category"].items()):
        print(f"  {cat}: {count}")


if __name__ == "__main__":
    main()
