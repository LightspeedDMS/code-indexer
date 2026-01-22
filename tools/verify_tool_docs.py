#!/usr/bin/env python3
"""
Verify MCP tool documentation completeness and validity.

Story #14: Externalize MCP Tool Documentation to Markdown Files
AC4: CI Gate Validation - Verify all 128 tools have valid documentation.

Usage:
    python3 tools/verify_tool_docs.py [--docs-dir PATH]

Exit codes:
    0 - All verifications passed
    1 - One or more verifications failed
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, Any, List

import yaml


DEFAULT_DOCS_DIR = Path("src/code_indexer/server/mcp/tool_docs")

REQUIRED_FRONTMATTER_FIELDS = ["name", "category", "required_permission", "tl_dr"]


def count_md_files(docs_dir: Path) -> int:
    """Count all .md files in docs directory."""
    count = 0
    for category_dir in docs_dir.iterdir():
        if category_dir.is_dir():
            count += len(list(category_dir.glob("*.md")))
    return count


def get_all_tool_names_from_docs(docs_dir: Path) -> List[str]:
    """Get all tool names from doc files."""
    tool_names = []
    for category_dir in docs_dir.iterdir():
        if not category_dir.is_dir():
            continue
        for md_file in category_dir.glob("*.md"):
            # Tool name is the filename without extension
            tool_names.append(md_file.stem)
    return tool_names


def verify_file_count(docs_dir: Path, tool_registry: Dict[str, Any]) -> Dict[str, Any]:
    """Verify the number of .md files matches the registry count."""
    expected_count = len(tool_registry)
    actual_count = count_md_files(docs_dir)

    if actual_count == expected_count:
        return {
            "success": True,
            "message": f"File count matches: {actual_count} files for {expected_count} tools",
        }
    else:
        return {
            "success": False,
            "message": f"File count mismatch: found {actual_count} files, expected {expected_count} tools",
        }


def verify_frontmatter(docs_dir: Path) -> Dict[str, Any]:
    """Verify all .md files have valid YAML frontmatter."""
    errors = []
    valid_count = 0

    for category_dir in docs_dir.iterdir():
        if not category_dir.is_dir():
            continue
        for md_file in category_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")

                if not content.startswith("---"):
                    errors.append(f"{md_file.name}: Missing frontmatter delimiter")
                    continue

                parts = content.split("---", 2)
                if len(parts) < 3:
                    errors.append(f"{md_file.name}: Invalid frontmatter format")
                    continue

                frontmatter = yaml.safe_load(parts[1])
                if frontmatter is None:
                    errors.append(f"{md_file.name}: Empty frontmatter")
                    continue

                # Check required fields
                for field in REQUIRED_FRONTMATTER_FIELDS:
                    if field not in frontmatter:
                        errors.append(
                            f"{md_file.name}: Missing required field '{field}'"
                        )
                        break
                else:
                    valid_count += 1

            except yaml.YAMLError as e:
                errors.append(f"{md_file.name}: Invalid YAML - {e}")
            except Exception as e:
                errors.append(f"{md_file.name}: Error reading file - {e}")

    if errors:
        return {
            "success": False,
            "message": f"Found {len(errors)} frontmatter errors",
            "errors": errors,
            "valid_count": valid_count,
        }
    else:
        return {
            "success": True,
            "message": f"All {valid_count} files have valid frontmatter",
            "valid_count": valid_count,
        }


def verify_registry_coverage(
    docs_dir: Path, tool_registry: Dict[str, Any]
) -> Dict[str, Any]:
    """Verify all tools in registry have corresponding doc files."""
    doc_tool_names = set(get_all_tool_names_from_docs(docs_dir))
    registry_tool_names = set(tool_registry.keys())

    missing = registry_tool_names - doc_tool_names
    extra = doc_tool_names - registry_tool_names

    if missing or extra:
        result = {
            "success": False,
            "message": f"Coverage mismatch: {len(missing)} missing, {len(extra)} extra",
            "missing": list(sorted(missing)),
            "extra": list(sorted(extra)),
        }
    else:
        result = {
            "success": True,
            "message": f"Full coverage: all {len(registry_tool_names)} tools documented",
            "missing": [],
            "extra": [],
        }

    return result


def verify_all(docs_dir: Path, tool_registry: Dict[str, Any]) -> Dict[str, Any]:
    """Run all verification checks."""
    file_count_result = verify_file_count(docs_dir, tool_registry)
    frontmatter_result = verify_frontmatter(docs_dir)
    coverage_result = verify_registry_coverage(docs_dir, tool_registry)

    all_success = (
        file_count_result["success"]
        and frontmatter_result["success"]
        and coverage_result["success"]
    )

    return {
        "success": all_success,
        "file_count": file_count_result,
        "frontmatter": frontmatter_result,
        "coverage": coverage_result,
    }


def main(argv: List[str] = None) -> int:
    """Main entry point for CLI usage."""
    parser = argparse.ArgumentParser(description="Verify MCP tool documentation")
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=DEFAULT_DOCS_DIR,
        help="Path to tool_docs directory",
    )
    args = parser.parse_args(argv)

    docs_dir = args.docs_dir

    if not docs_dir.exists():
        print(f"ERROR: Tool docs directory not found: {docs_dir}")
        return 1

    # Import registry
    sys.path.insert(0, "src")
    from code_indexer.server.mcp.tools import TOOL_REGISTRY

    print(f"Verifying tool documentation in: {docs_dir}")
    print(f"Registry contains: {len(TOOL_REGISTRY)} tools\n")

    result = verify_all(docs_dir, TOOL_REGISTRY)

    # Print results
    print("File Count Check:")
    print(f"  {result['file_count']['message']}")
    print(f"  Status: {'PASS' if result['file_count']['success'] else 'FAIL'}\n")

    print("Frontmatter Validation:")
    print(f"  {result['frontmatter']['message']}")
    if not result["frontmatter"]["success"] and "errors" in result["frontmatter"]:
        for error in result["frontmatter"]["errors"][:10]:  # Show first 10
            print(f"    - {error}")
        if len(result["frontmatter"]["errors"]) > 10:
            print(f"    ... and {len(result['frontmatter']['errors']) - 10} more")
    print(f"  Status: {'PASS' if result['frontmatter']['success'] else 'FAIL'}\n")

    print("Registry Coverage Check:")
    print(f"  {result['coverage']['message']}")
    if result["coverage"].get("missing"):
        print(f"  Missing tools: {', '.join(result['coverage']['missing'][:10])}")
    if result["coverage"].get("extra"):
        print(f"  Extra docs: {', '.join(result['coverage']['extra'][:10])}")
    print(f"  Status: {'PASS' if result['coverage']['success'] else 'FAIL'}\n")

    if result["success"]:
        print("VERIFICATION PASSED: All checks successful")
        return 0
    else:
        print("VERIFICATION FAILED: One or more checks failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
