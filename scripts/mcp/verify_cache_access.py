#!/usr/bin/env python3
"""
Story #987 AC5: CI check for ToolDocLoader cache encapsulation.

Verifies that no file outside tool_doc_loader.py directly accesses
`loader._cache` or `_singleton_loader._cache` using Python AST inspection.

Usage:
    python3 scripts/mcp/verify_cache_access.py [--src-root PATH]

Exit codes:
    0 - No violations found
    1 - One or more violations found
"""

import ast
import argparse
import sys
from pathlib import Path
from typing import List, Tuple


DEFAULT_SRC_ROOT = Path("src")

# The one file allowed to access _cache directly
ALLOWED_FILE_NAME = "tool_doc_loader.py"

# Attribute name that is forbidden outside tool_doc_loader.py
FORBIDDEN_ATTRIBUTE = "_cache"

# Variable names on which ._cache access is forbidden in all other files
FORBIDDEN_OBJECTS = {"loader", "_singleton_loader"}


class CacheAccessVisitor(ast.NodeVisitor):
    """AST visitor that detects forbidden _cache attribute accesses.

    Only flags accesses of the form `loader._cache` or `_singleton_loader._cache`
    where the object name is in FORBIDDEN_OBJECTS.
    """

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: List[Tuple[int, str]] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Check for loader._cache or _singleton_loader._cache attribute access."""
        if (
            node.attr == FORBIDDEN_ATTRIBUTE
            and isinstance(node.value, ast.Name)
            and node.value.id in FORBIDDEN_OBJECTS
        ):
            source_expr = node.value.id
            line = node.lineno
            self.violations.append(
                (
                    line,
                    f"Direct access to ._cache on '{source_expr}' at line {line}. "
                    "Use get_extended_description() or other public accessor instead.",
                )
            )
        self.generic_visit(node)


def check_file(py_file: Path) -> List[Tuple[int, str]]:
    """Parse a Python file and return all _cache access violations."""
    try:
        source = py_file.read_text(encoding="utf-8")
    except OSError as e:
        return [(0, f"Could not read file: {e}")]

    try:
        tree = ast.parse(source, filename=str(py_file))
    except SyntaxError as e:
        return [(0, f"Syntax error: {e}")]

    visitor = CacheAccessVisitor(str(py_file))
    visitor.visit(tree)
    return visitor.violations


def find_python_files(src_root: Path) -> List[Path]:
    """Recursively find all .py files under src_root."""
    return sorted(src_root.rglob("*.py"))


def verify_cache_access(src_root: Path) -> List[Tuple[Path, int, str]]:
    """Run cache access verification across all Python files.

    Files named ALLOWED_FILE_NAME are skipped (tool_doc_loader.py is
    the only file permitted to access _cache directly).

    Returns:
        List of (file_path, line_number, message) tuples for each violation.
    """
    all_violations: List[Tuple[Path, int, str]] = []

    for py_file in find_python_files(src_root):
        if py_file.name == ALLOWED_FILE_NAME:
            continue

        violations = check_file(py_file)
        for line, message in violations:
            all_violations.append((py_file, line, message))

    return all_violations


def main(argv: List[str] = None) -> int:  # type: ignore[assignment]
    """Main entry point for CLI usage."""
    parser = argparse.ArgumentParser(
        description="Verify ToolDocLoader cache encapsulation (Story #987 AC5)"
    )
    parser.add_argument(
        "--src-root",
        type=Path,
        default=DEFAULT_SRC_ROOT,
        help="Path to source root directory to scan",
    )
    args = parser.parse_args(argv)

    src_root = args.src_root
    if not src_root.exists():
        print(f"ERROR: Source root not found: {src_root}")
        return 1

    print(f"Checking cache access encapsulation in: {src_root}")
    violations = verify_cache_access(src_root)

    if not violations:
        print(
            "PASS: No direct _cache access violations found outside tool_doc_loader.py"
        )
        return 0

    print(f"FAIL: Found {len(violations)} direct _cache access violation(s):\n")
    for file_path, line, message in violations:
        print(f"  {file_path}:{line}: {message}")

    print(
        "\nFix: Use loader.get_extended_description(tool_name) or other public "
        "accessors instead of accessing ._cache directly."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
