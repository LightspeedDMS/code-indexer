#!/usr/bin/env python3
"""
Async to Sync Converter Script

Automates conversion of async functions to sync functions when they don't
actually use await. This optimizes FastAPI/uvicorn performance by allowing
sync handlers to run in the thread pool instead of blocking the event loop.

Usage:
    # Scan mode - find async functions without await
    python async_to_sync_converter.py scan src/code_indexer/server/

    # Preview what would be converted (dry run)
    python async_to_sync_converter.py convert src/code_indexer/server/app.py --dry-run

    # Convert all async-without-await in a directory
    python async_to_sync_converter.py convert src/code_indexer/server/ --all

    # Convert and fix callers (including tests)
    python async_to_sync_converter.py convert src/ --all --fix-callers --caller-paths "src/,tests/"

Epic #49: Server Async→Sync Optimization
"""

import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# Default functions that should stay async (context managers, SSE, middleware)
DEFAULT_EXCLUDE_FUNCS = {
    '__aenter__', '__aexit__', 'dispatch',
    'sse_event_generator', 'mcp_sse_endpoint', 'mcp_public_sse_endpoint',
}


@dataclass
class AsyncFunctionInfo:
    """Information about an async function."""
    name: str
    lineno: int
    end_lineno: int
    filepath: str
    has_await: bool
    decorators: List[str] = field(default_factory=list)

    @property
    def relative_path(self) -> str:
        """Get path relative to current directory."""
        try:
            return str(Path(self.filepath).relative_to(Path.cwd()))
        except ValueError:
            return self.filepath


class AsyncFunctionFinder(ast.NodeVisitor):
    """AST visitor to find async functions and their await usage."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.functions: List[AsyncFunctionInfo] = []
        self._current_function: Optional[ast.AsyncFunctionDef] = None
        self._current_has_await = False

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        """Visit async function definition."""
        parent_function = self._current_function
        parent_has_await = self._current_has_await

        self._current_function = node
        self._current_has_await = False
        self.generic_visit(node)

        decorators = self._extract_decorators(node)
        info = AsyncFunctionInfo(
            name=node.name,
            lineno=node.lineno,
            end_lineno=node.end_lineno or node.lineno,
            filepath=self.filepath,
            has_await=self._current_has_await,
            decorators=decorators,
        )
        self.functions.append(info)

        self._current_function = parent_function
        self._current_has_await = parent_has_await

    def _extract_decorators(self, node: ast.AsyncFunctionDef) -> List[str]:
        """Extract decorator names from function node."""
        decorators = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name):
                decorators.append(dec.id)
            elif isinstance(dec, ast.Attribute):
                decorators.append(dec.attr)
            elif isinstance(dec, ast.Call):
                if isinstance(dec.func, ast.Name):
                    decorators.append(dec.func.id)
                elif isinstance(dec.func, ast.Attribute):
                    decorators.append(dec.func.attr)
        return decorators

    def visit_Await(self, node: ast.Await):
        """Mark current function as having await."""
        if self._current_function is not None:
            self._current_has_await = True
        self.generic_visit(node)


class CallerFinder(ast.NodeVisitor):
    """AST visitor to find calls to specific functions that are awaited."""

    def __init__(self, filepath: str, target_functions: Set[str]):
        self.filepath = filepath
        self.target_functions = target_functions
        self.await_calls: List[Tuple[int, str]] = []

    def visit_Await(self, node: ast.Await):
        """Visit await expressions."""
        if isinstance(node.value, ast.Call):
            func_name = self._get_call_name(node.value)
            if func_name and func_name in self.target_functions:
                self.await_calls.append((node.lineno, func_name))
        self.generic_visit(node)

    def _get_call_name(self, call: ast.Call) -> Optional[str]:
        """Extract function name from a Call node."""
        if isinstance(call.func, ast.Name):
            return call.func.id
        elif isinstance(call.func, ast.Attribute):
            return call.func.attr
        return None


def scan_file(filepath: str) -> List[AsyncFunctionInfo]:
    """Scan a file for async functions."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        tree = ast.parse(content, filename=filepath)
        finder = AsyncFunctionFinder(filepath)
        finder.visit(tree)
        return finder.functions
    except (SyntaxError, UnicodeDecodeError) as e:
        print(f"Warning: Could not parse {filepath}: {e}", file=sys.stderr)
        return []


def scan_directory(
    directory: str,
    exclude_patterns: Optional[List[str]] = None,
) -> List[AsyncFunctionInfo]:
    """Scan directory for async functions."""
    exclude_patterns = exclude_patterns or []
    all_functions = []

    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d != "__pycache__"]

        for filename in files:
            if not filename.endswith('.py'):
                continue

            skip = any(p in filename or p in root for p in exclude_patterns)
            if skip:
                continue

            filepath = os.path.join(root, filename)
            all_functions.extend(scan_file(filepath))

    return all_functions


def convert_function_in_file(
    filepath: str,
    function_name: str,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """Convert a single async function to sync in a file."""
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines(keepends=True)

    pattern = rf'^(\s*)async\s+def\s+{re.escape(function_name)}\s*\('
    modified = False
    new_lines = []

    for line in lines:
        if re.match(pattern, line):
            new_line = re.sub(r'^(\s*)async\s+def\s+', r'\1def ', line)
            new_lines.append(new_line)
            modified = True
        else:
            new_lines.append(line)

    if not modified:
        return False, f"Function {function_name} not found in {filepath}"

    if not dry_run:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(''.join(new_lines))

    action = "Would convert" if dry_run else "Converted"
    return True, f"{action} {function_name} in {filepath}"


def remove_await_from_callers(
    directories: List[str],
    function_names: Set[str],
    dry_run: bool = False,
) -> List[str]:
    """Find and remove 'await' from calls to converted functions."""
    modifications = []

    for directory in directories:
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d != "__pycache__"]

            for filename in files:
                if not filename.endswith('.py'):
                    continue

                filepath = os.path.join(root, filename)
                mods = _process_caller_file(filepath, function_names, dry_run)
                modifications.extend(mods)

    return modifications


def _process_caller_file(
    filepath: str,
    function_names: Set[str],
    dry_run: bool,
) -> List[str]:
    """Process a single file for caller fixes."""
    modifications = []

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        tree = ast.parse(content, filename=filepath)
        finder = CallerFinder(filepath, function_names)
        finder.visit(tree)

        if not finder.await_calls:
            return []

        lines = content.splitlines(keepends=True)
        modified = False

        for lineno, func_name in sorted(finder.await_calls, reverse=True):
            idx = lineno - 1
            if idx < len(lines):
                line = lines[idx]
                new_line = re.sub(
                    rf'\bawait\s+(\w+\.)*{re.escape(func_name)}\s*\(',
                    rf'\1{func_name}(',
                    line
                )
                if new_line != line:
                    lines[idx] = new_line
                    modified = True
                    action = "Would remove" if dry_run else "Removed"
                    modifications.append(
                        f"{action} await from {func_name} at {filepath}:{lineno}"
                    )

        if modified and not dry_run:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(''.join(lines))

    except (SyntaxError, UnicodeDecodeError) as e:
        print(f"Warning: Could not process {filepath}: {e}", file=sys.stderr)

    return modifications


def handle_scan_command(args) -> None:
    """Handle the scan command."""
    path = Path(args.path)

    if path.is_file():
        functions = scan_file(str(path))
    else:
        functions = scan_directory(str(path), args.exclude)

    if not args.show_all:
        functions = [f for f in functions if not f.has_await]

    if args.json:
        _output_scan_json(functions)
    else:
        _output_scan_text(functions, args.show_all)


def _output_scan_json(functions: List[AsyncFunctionInfo]) -> None:
    """Output scan results as JSON."""
    import json
    data = [
        {
            'name': f.name,
            'file': f.relative_path,
            'line': f.lineno,
            'has_await': f.has_await,
            'decorators': f.decorators,
        }
        for f in functions
    ]
    print(json.dumps(data, indent=2))


def _output_scan_text(functions: List[AsyncFunctionInfo], show_all: bool) -> None:
    """Output scan results as text."""
    label = 'total' if show_all else 'without await'
    print(f"Found {len(functions)} async functions {label}:\n")

    by_file: Dict[str, List[AsyncFunctionInfo]] = {}
    for f in functions:
        by_file.setdefault(f.relative_path, []).append(f)

    for filepath, funcs in sorted(by_file.items()):
        print(f"{filepath}:")
        for f in sorted(funcs, key=lambda x: x.lineno):
            status = "HAS AWAIT" if f.has_await else "NO AWAIT"
            decs = f", decorators: {f.decorators}" if f.decorators else ""
            print(f"  {f.lineno}: {f.name} [{status}]{decs}")
        print()


def handle_convert_command(args) -> None:
    """Handle the convert command."""
    path = Path(args.path)
    exclude_funcs = _build_exclude_set(args.exclude_funcs)
    exclude_files = args.exclude_files.split(',') if args.exclude_files else []

    target_functions, by_file = _get_conversion_targets(
        path, args, exclude_funcs, exclude_files
    )

    if not target_functions:
        print("No functions to convert.", file=sys.stderr)
        return

    print(f"Converting {len(target_functions)} functions...")
    if args.dry_run:
        print("(DRY RUN - no changes will be made)\n")

    successes, failures = _perform_conversions(path, args, target_functions, by_file)
    print(f"\nConversions: {successes} successful, {failures} failed")

    if args.fix_callers and target_functions:
        _fix_caller_await(args, target_functions, path)


def _build_exclude_set(exclude_arg: Optional[str]) -> Set[str]:
    """Build the set of functions to exclude from conversion."""
    exclude_funcs = set(exclude_arg.split(',')) if exclude_arg else set()
    exclude_funcs.update(DEFAULT_EXCLUDE_FUNCS)
    return exclude_funcs


def _get_conversion_targets(
    path: Path,
    args,
    exclude_funcs: Set[str],
    exclude_files: List[str],
) -> Tuple[Set[str], Dict[str, List[AsyncFunctionInfo]]]:
    """Get the functions to convert and group them by file."""
    by_file: Dict[str, List[AsyncFunctionInfo]] = {}

    if args.functions:
        return set(args.functions.split(',')), by_file

    if not args.all:
        print("Error: Must specify --functions or --all", file=sys.stderr)
        sys.exit(1)

    if path.is_file():
        functions = scan_file(str(path))
    else:
        functions = scan_directory(str(path), exclude_patterns=exclude_files)

    functions = [
        f for f in functions
        if not f.has_await and f.name not in exclude_funcs
    ]

    for f in functions:
        by_file.setdefault(f.filepath, []).append(f)

    return {f.name for f in functions}, by_file


def _perform_conversions(
    path: Path,
    args,
    target_functions: Set[str],
    by_file: Dict[str, List[AsyncFunctionInfo]],
) -> Tuple[int, int]:
    """Perform the actual conversions."""
    successes = 0
    failures = 0

    if args.functions:
        for func_name in target_functions:
            success, msg = convert_function_in_file(str(path), func_name, args.dry_run)
            print(f"  {msg}")
            successes += 1 if success else 0
            failures += 0 if success else 1
    else:
        for filepath, funcs in sorted(by_file.items()):
            for f in funcs:
                success, msg = convert_function_in_file(filepath, f.name, args.dry_run)
                print(f"  {msg}")
                successes += 1 if success else 0
                failures += 0 if success else 1

    return successes, failures


def _fix_caller_await(args, target_functions: Set[str], path: Path) -> None:
    """Fix await calls in caller files including tests."""
    print(f"\nFixing callers (including tests)...")

    if args.caller_paths:
        search_paths = [p.strip() for p in args.caller_paths.split(',')]
    else:
        search_paths = [str(path) if path.is_dir() else str(path.parent)]
        # Auto-include tests directory if it exists
        tests_dir = Path('tests')
        if tests_dir.exists() and str(tests_dir) not in search_paths:
            search_paths.append(str(tests_dir))

    modifications = remove_await_from_callers(search_paths, target_functions, args.dry_run)
    for mod in modifications:
        print(f"  {mod}")
    print(f"\nCaller fixes: {len(modifications)}")


def main():
    parser = argparse.ArgumentParser(
        description="Convert async functions to sync when they don't use await",
    )
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # Scan command
    scan_parser = subparsers.add_parser('scan', help='Scan for async functions')
    scan_parser.add_argument('path', help='File or directory to scan')
    scan_parser.add_argument('--exclude', nargs='*', default=[], help='Patterns to exclude')
    scan_parser.add_argument('--show-all', action='store_true', help='Show all async functions')
    scan_parser.add_argument('--json', action='store_true', help='Output as JSON')

    # Convert command
    conv_parser = subparsers.add_parser('convert', help='Convert async functions to sync')
    conv_parser.add_argument('path', help='File or directory to convert')
    conv_parser.add_argument('--functions', help='Comma-separated function names')
    conv_parser.add_argument('--all', action='store_true', help='Convert all without await')
    conv_parser.add_argument('--dry-run', action='store_true', help='Preview changes only')
    conv_parser.add_argument('--fix-callers', action='store_true', help='Fix await in callers')
    conv_parser.add_argument('--caller-paths', help='Paths to search for callers (comma-sep)')
    conv_parser.add_argument('--exclude-funcs', help='Function names to exclude (comma-sep)')
    conv_parser.add_argument('--exclude-files', help='File patterns to exclude (comma-sep)')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == 'scan':
        handle_scan_command(args)
    elif args.command == 'convert':
        handle_convert_command(args)


if __name__ == '__main__':
    main()
