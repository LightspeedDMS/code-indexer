#!/usr/bin/env python3
"""
Automated migration tool for logger.warning/error/critical statements.

Transforms old-style logging to new format_error_log/get_log_extra pattern.
Uses pre-allocated error codes from ERROR_REGISTRY.
"""

import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# Constants
MAX_MESSAGE_LENGTH = 80
MAX_CONTEXT_VARS = 3

# Map file patterns to subsystem prefixes
SUBSYSTEM_MAP = {
    "mcp/": "MCP",
    "git": "GIT",
    "auth/": "AUTH",
    "routers/git.py": "GIT",
    "routers/files.py": "WEB",
    "web/": "WEB",
    "repositories/": "REPO",
    "app.py": "APP",
    "query/": "QUERY",
    "storage/": "STORE",
    "services/": "SVC",
    "validation/": "VALID",
    "auto_update/": "DEPLOY",
    "scip": "SCIP",
    "telemetry/": "TELEM",
    "installer.py": "APP",
    "cache": "CACHE",
}

# Track error code allocation
ERROR_CODE_COUNTERS: Dict[str, Dict[str, int]] = {}


def determine_subsystem(file_path: str) -> str:
    """Determine subsystem prefix from file path."""
    for pattern, subsystem in SUBSYSTEM_MAP.items():
        if pattern in file_path:
            return subsystem
    return "APP"  # Default fallback


def get_next_error_code(subsystem: str, category: str = "GENERAL") -> str:
    """Get next available error code for subsystem-category."""
    if subsystem not in ERROR_CODE_COUNTERS:
        ERROR_CODE_COUNTERS[subsystem] = {}

    if category not in ERROR_CODE_COUNTERS[subsystem]:
        ERROR_CODE_COUNTERS[subsystem][category] = 0

    ERROR_CODE_COUNTERS[subsystem][category] += 1
    number = ERROR_CODE_COUNTERS[subsystem][category]

    return f"{subsystem}-{category}-{number:03d}"


def extract_message_and_context(log_call: str) -> Tuple[str, List[str]]:
    """
    Extract message and context variables from logger call.

    Returns:
        (message_template, context_variables)
    """
    match = re.search(r'logger\.(warning|error|critical)\((.*)\)', log_call, re.DOTALL)
    if not match:
        return ("Migration required", [])

    args = match.group(2).strip()

    # Check if it's an f-string
    if args.startswith('f"') or args.startswith("f'"):
        variables = re.findall(r'\{([^}]+)\}', args)
        message = args

        # Remove f-string syntax
        if message.startswith('f"'):
            message = message[2:]
        elif message.startswith("f'"):
            message = message[2:]

        # Remove trailing quote
        if message.endswith('"'):
            message = message[:-1]
        elif message.endswith("'"):
            message = message[:-1]

        # Replace {var} with var description
        for var in variables:
            message = message.replace(f"{{{var}}}", var)

        return (message[:MAX_MESSAGE_LENGTH], variables)

    # Plain string
    if args.startswith('"') or args.startswith("'"):
        quote_char = args[0]
        end_idx = args.find(quote_char, 1)
        if end_idx > 0:
            message = args[1:end_idx]
            return (message[:MAX_MESSAGE_LENGTH], [])

    return ("TODO: Complex logging pattern", [])


def migrate_logger_call(line_content: str, subsystem: str) -> str:
    """
    Migrate a single logger.warning/error/critical call.

    Args:
        line_content: Original line with logger call
        subsystem: Subsystem prefix to use

    Returns:
        Migrated logger call
    """
    # Get error code
    error_code = get_next_error_code(subsystem, "GENERAL")

    # Extract message and context
    message, context_vars = extract_message_and_context(line_content)

    # Build context kwargs
    context_str = ""
    if context_vars:
        limited_vars = context_vars[:MAX_CONTEXT_VARS]
        context_parts = [f"{var}={var}" for var in limited_vars]
        context_str = ", " + ", ".join(context_parts)

    # Detect indentation
    indent_match = re.match(r'^(\s+)', line_content)
    indent = indent_match.group(1) if indent_match else ""

    # Determine method
    if "logger.warning(" in line_content:
        method = "warning"
    elif "logger.error(" in line_content:
        method = "error"
    else:
        method = "critical"

    # Format new call
    new_call = f'{indent}logger.{method}(\n'
    new_call += f'{indent}    format_error_log("{error_code}", "{message}"{context_str}),\n'
    new_call += f'{indent}    extra=get_log_extra("{error_code}")\n'
    new_call += f'{indent})'

    return new_call


def check_imports(content: str) -> Tuple[bool, bool]:
    """Check if format_error_log and get_log_extra are imported."""
    has_format = "format_error_log" in content
    has_extra = "get_log_extra" in content
    return (has_format, has_extra)


def add_imports(content: str) -> str:
    """Add missing imports to file."""
    lines = content.split('\n')

    # Check if imports already exist
    has_format, has_extra = check_imports(content)

    if has_format and has_extra:
        return content

    # Find insertion point
    import_idx = 0
    for i, line in enumerate(lines):
        if line.startswith('import ') or line.startswith('from '):
            import_idx = i + 1
        elif import_idx > 0 and not line.strip():
            break

    # Insert import
    import_line = "from code_indexer.server.logging_utils import format_error_log, get_log_extra"

    if import_line not in content:
        lines.insert(import_idx, import_line)

    return '\n'.join(lines)


def migrate_file(file_path: Path) -> Tuple[int, str]:
    """
    Migrate a single file.

    Returns:
        (num_migrations, new_content)
    """
    try:
        content = file_path.read_text(encoding='utf-8')
    except (IOError, UnicodeDecodeError) as e:
        print(f"Error reading {file_path}: {e}", file=sys.stderr)
        return (0, "")

    subsystem = determine_subsystem(str(file_path))

    lines = content.split('\n')
    new_lines = []
    migrations = 0

    for i, line in enumerate(lines):
        # Check if line has unmigrated logger call
        if re.search(r'logger\.(warning|error|critical)\(', line):
            # Check if already migrated
            if 'format_error_log' in line:
                new_lines.append(line)
                continue

            # Check if this is single-line
            if not (line.strip().endswith(')') or line.strip().endswith('))') or line.strip().endswith('),')):
                new_lines.append(line)
                continue

            # Single-line call - migrate it
            try:
                new_call = migrate_logger_call(line, subsystem)
                new_lines.append(new_call)
                migrations += 1
            except Exception as e:
                print(f"  Warning: Could not migrate line {i+1}: {e}", file=sys.stderr)
                new_lines.append(line)
        else:
            new_lines.append(line)

    if migrations > 0:
        new_content = '\n'.join(new_lines)
        new_content = add_imports(new_content)
        return (migrations, new_content)

    return (0, content)


def main():
    """Main migration entry point."""
    if len(sys.argv) < 2:
        print("Usage: python3 migrate_logging.py <file_or_directory>")
        sys.exit(1)

    path = Path(sys.argv[1])

    if not path.exists():
        print(f"Error: {path} does not exist", file=sys.stderr)
        sys.exit(1)

    # Collect files
    files_to_process = []
    if path.is_file():
        files_to_process.append(path)
    else:
        files_to_process = list(path.rglob("*.py"))

    total_migrations = 0

    for file_path in sorted(files_to_process):
        if 'test' in str(file_path) or 'migrate_logging.py' in str(file_path):
            continue

        print(f"Processing {file_path}...")
        migrations, new_content = migrate_file(file_path)

        if migrations > 0:
            try:
                file_path.write_text(new_content, encoding='utf-8')
                print(f"  Migrated {migrations} statements")
                total_migrations += migrations
            except (IOError, OSError) as e:
                print(f"  Error writing {file_path}: {e}", file=sys.stderr)
                continue
        else:
            print(f"  No migrations needed")

    print(f"\nTotal migrations: {total_migrations}")

    # Print summary
    print("\nError code allocation summary:")
    for subsystem in sorted(ERROR_CODE_COUNTERS.keys()):
        for category, count in ERROR_CODE_COUNTERS[subsystem].items():
            print(f"  {subsystem}-{category}: {count} codes allocated")


if __name__ == "__main__":
    main()
