#!/usr/bin/env python3
"""
Migrate all logger.warning/error/critical calls to use format_error_log pattern.

This script:
1. Parses Python files using AST to find logger calls
2. Transforms them to use format_error_log with unique error codes
3. Assigns error codes from pre-allocated placeholders in ERROR_REGISTRY
4. Preserves code formatting and handles multi-line patterns
"""

import ast
import re
from pathlib import Path
from typing import Dict, List, Tuple
import sys


REQUIRED_ERROR_CODES = 808


class LoggerCallTransformer(ast.NodeTransformer):
    """AST transformer to convert logger calls to format_error_log pattern."""

    def __init__(self, error_codes: List[str], file_path: str):
        self.error_codes = error_codes
        self.error_code_index = 0
        self.file_path = file_path
        self.transformations: List[Tuple[int, int, str, str]] = []  # (start_line, end_line, error_code, log_level)
        self.has_format_error_log_import = False

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.ImportFrom:
        """Check if format_error_log is already imported."""
        if node.module and 'error_handler' in node.module:
            for alias in node.names:
                if alias.name == 'format_error_log':
                    self.has_format_error_log_import = True
        return node

    def visit_Call(self, node: ast.Call) -> ast.Call:
        """Transform logger.warning/error/critical calls."""
        # Check if this is a logger call
        if isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                if node.func.value.id == 'logger' and node.func.attr in ['warning', 'error', 'critical']:
                    # Check if already using format_error_log (skip if so)
                    if self._uses_format_error_log(node):
                        return node

                    # Get the next available error code
                    if self.error_code_index >= len(self.error_codes):
                        print(f"ERROR: Ran out of error codes for {self.file_path}")
                        return node

                    error_code = self.error_codes[self.error_code_index]
                    self.error_code_index += 1

                    # Store transformation
                    self.transformations.append((node.lineno, node.end_lineno, error_code, node.func.attr))

        self.generic_visit(node)
        return node

    def _uses_format_error_log(self, node: ast.Call) -> bool:
        """Check if the call already uses format_error_log."""
        # Check if any argument is a call to format_error_log
        for arg in node.args:
            if isinstance(arg, ast.Call):
                if isinstance(arg.func, ast.Name) and arg.func.id == 'format_error_log':
                    return True
        return False


def get_available_error_codes() -> List[str]:
    """Extract available error codes from ERROR_REGISTRY."""
    # Find project root (where src/ directory exists)
    script_dir = Path(__file__).parent
    project_root = script_dir.parent.parent
    error_codes_path = project_root / 'src' / 'code_indexer' / 'server' / 'error_codes.py'

    if not error_codes_path.exists():
        print(f"ERROR: Cannot find error_codes.py at {error_codes_path}")
        sys.exit(1)

    content = error_codes_path.read_text()

    # Find all error codes with description="TODO"
    pattern = r'ErrorDefinition\(code="([^"]+)",\s*description="TODO"'
    matches = re.findall(pattern, content)

    print(f"Found {len(matches)} available error codes")
    return matches


def analyze_file(file_path: Path, error_codes: List[str]) -> Tuple[bool, List[Tuple[int, int, str, str]], bool]:
    """
    Analyze a file and identify logger calls to transform.

    Returns:
        (needs_transform, transformations, has_import)
    """
    try:
        content = file_path.read_text()
        tree = ast.parse(content, filename=str(file_path))

        transformer = LoggerCallTransformer(error_codes, str(file_path))
        transformer.visit(tree)

        return (
            len(transformer.transformations) > 0,
            transformer.transformations,
            transformer.has_format_error_log_import
        )
    except SyntaxError as e:
        print(f"SYNTAX ERROR in {file_path}: {e}")
        return False, [], False


def find_matching_paren(text: str, start_pos: int) -> int:
    """
    Find matching closing parenthesis with string-aware parsing.

    Args:
        text: Text to search
        start_pos: Position after opening paren

    Returns:
        Position of matching closing paren, or 0 if not found
    """
    paren_depth = 1
    in_string = False
    string_char = None
    escape_next = False

    for i, char in enumerate(text[start_pos:], start=start_pos):
        if escape_next:
            escape_next = False
            continue

        if char == '\\':
            escape_next = True
            continue

        # Track string boundaries
        if char in ('"', "'") and not in_string:
            in_string = True
            string_char = char
        elif char == string_char and in_string:
            in_string = False
            string_char = None

        # Only count parens outside of strings
        if not in_string:
            if char == '(':
                paren_depth += 1
            elif char == ')':
                paren_depth -= 1
                if paren_depth == 0:
                    return i - start_pos

    return 0


def extract_logger_call_content(original_call: str) -> str:
    """
    Extract the message content from a logger call.

    Args:
        original_call: Full logger call text

    Returns:
        Message content (everything inside the parentheses)
    """
    # Find the opening parenthesis
    match = re.search(r'logger\.\w+\s*\(', original_call)
    if not match:
        return ""

    call_start = match.end()
    rest_of_call = original_call[call_start:]

    # Find matching closing paren using string-aware parsing
    message_end = find_matching_paren(rest_of_call, 0)
    if message_end == 0:
        return ""

    return rest_of_call[:message_end].strip()


def build_new_logger_call(indent_str: str, log_level: str, error_code: str, message_content: str) -> str:
    """
    Build the new logger call with format_error_log.

    Args:
        indent_str: Indentation string
        log_level: Log level (warning/error/critical)
        error_code: Error code to use
        message_content: Original message content

    Returns:
        New logger call text
    """
    new_call = f'{indent_str}logger.{log_level}(format_error_log(\n'
    new_call += f'{indent_str}    "{error_code}",\n'
    new_call += f'{indent_str}    {message_content}\n'
    new_call += f'{indent_str}))\n'
    return new_call


def transform_file_content(content: str, transformations: List[Tuple[int, int, str, str]], has_import: bool) -> str:
    """
    Transform file content by replacing logger calls with format_error_log pattern.
    """
    lines = content.splitlines(keepends=True)

    # Sort transformations by line number (reverse order to avoid offset issues)
    transformations.sort(key=lambda x: x[0], reverse=True)

    for start_line, end_line, error_code, log_level in transformations:
        # Extract the original call (1-indexed to 0-indexed)
        start_idx = start_line - 1
        end_idx = end_line

        original_lines = lines[start_idx:end_idx]
        original_call = ''.join(original_lines)

        # Extract the message content
        message_content = extract_logger_call_content(original_call)
        if not message_content:
            continue

        # Get indentation from original line
        indent = len(original_lines[0]) - len(original_lines[0].lstrip())
        indent_str = ' ' * indent

        # Build new call
        new_call = build_new_logger_call(indent_str, log_level, error_code, message_content)

        # Replace lines
        lines[start_idx:end_idx] = [new_call]

    # Add import if needed
    if transformations and not has_import:
        # Find the last import statement
        import_insert_line = 0
        for i, line in enumerate(lines):
            if line.strip().startswith('import ') or line.strip().startswith('from '):
                import_insert_line = i + 1

        # Insert import after last import
        import_line = 'from code_indexer.server.logging.error_handler import format_error_log\n'
        lines.insert(import_insert_line, import_line)

    return ''.join(lines)


def migrate_files(target_dir: Path, error_codes: List[str], dry_run: bool = False) -> Dict[str, int]:
    """
    Migrate all Python files in target directory.

    Returns:
        Dictionary with migration statistics
    """
    stats = {
        'files_analyzed': 0,
        'files_transformed': 0,
        'statements_migrated': 0,
        'error_codes_used': 0
    }

    error_code_offset = 0

    for file_path in sorted(target_dir.rglob('*.py')):
        if '__pycache__' in str(file_path):
            continue

        stats['files_analyzed'] += 1

        # Get available error codes for this file
        available_codes = error_codes[error_code_offset:]

        needs_transform, transformations, has_import = analyze_file(file_path, available_codes)

        if needs_transform:
            stats['files_transformed'] += 1
            stats['statements_migrated'] += len(transformations)
            stats['error_codes_used'] += len(transformations)
            error_code_offset += len(transformations)

            print(f"Transforming {file_path}: {len(transformations)} statements")

            if not dry_run:
                content = file_path.read_text()
                new_content = transform_file_content(content, transformations, has_import)
                file_path.write_text(new_content)

    return stats


def main():
    """Main migration entry point."""
    import argparse

    parser = argparse.ArgumentParser(description='Migrate logger calls to format_error_log')
    parser.add_argument('--dry-run', action='store_true', help='Analyze only, do not modify files')
    parser.add_argument('--target', default='src/code_indexer/server', help='Target directory')

    args = parser.parse_args()

    print("=" * 80)
    print("Logger Call Migration Script")
    print("=" * 80)

    # Get available error codes
    error_codes = get_available_error_codes()

    if len(error_codes) < REQUIRED_ERROR_CODES:
        print(f"ERROR: Not enough error codes! Need {REQUIRED_ERROR_CODES}, have {len(error_codes)}")
        sys.exit(1)

    # Migrate files
    script_dir = Path(__file__).parent
    project_root = script_dir.parent.parent
    target_dir = project_root / args.target
    print(f"\nAnalyzing files in {target_dir}...")

    stats = migrate_files(target_dir, error_codes, dry_run=args.dry_run)

    print("\n" + "=" * 80)
    print("Migration Statistics")
    print("=" * 80)
    print(f"Files analyzed:       {stats['files_analyzed']}")
    print(f"Files transformed:    {stats['files_transformed']}")
    print(f"Statements migrated:  {stats['statements_migrated']}")
    print(f"Error codes used:     {stats['error_codes_used']}")
    print(f"Error codes remaining: {len(error_codes) - stats['error_codes_used']}")

    if args.dry_run:
        print("\nDRY RUN - No files were modified")
    else:
        print("\nMigration complete!")

    return 0


if __name__ == '__main__':
    sys.exit(main())
