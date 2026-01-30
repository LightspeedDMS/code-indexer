#!/usr/bin/env python3
"""
Analyze log statements in CIDX server code.

Scans all Python files for logger.warning/error/critical calls,
categorizes them by subsystem, and generates a structured inventory
for populating the ERROR_REGISTRY.

Usage:
    python3 scripts/analysis/analyze_log_statements.py > .tmp/log_inventory.txt
"""

import re
import sys
from pathlib import Path
from typing import List, Tuple


# Subsystem mapping based on directory structure
SUBSYSTEM_MAP = {
    "auth": "AUTH",
    "git": "GIT",
    "mcp": "MCP",
    "cache": "CACHE",
    "repositories": "REPO",
    "query": "QUERY",
    "validation": "VALID",
    "auto_update": "DEPLOY",
    "routes": "WEB",
    "routers": "WEB",
    "web": "WEB",
    "services": "SVC",
    "storage": "STORE",
    "telemetry": "TELEM",
    "app": "APP",
    "installer": "APP",
}


def determine_subsystem(file_path: Path) -> str:
    """Determine subsystem prefix from file path."""
    parts = file_path.parts
    server_idx = parts.index("server") if "server" in parts else -1

    if server_idx >= 0 and server_idx + 1 < len(parts):
        subdir = parts[server_idx + 1]
        return SUBSYSTEM_MAP.get(subdir, "APP")

    return "APP"


def extract_log_statements(file_path: Path) -> List[Tuple[int, str, str]]:
    """
    Extract log statements from a Python file.

    Returns:
        List of (line_number, log_level, full_line) tuples
    """
    results = []

    try:
        content = file_path.read_text()
        lines = content.split("\n")

        for i, line in enumerate(lines, start=1):
            # Match logger.warning/error/critical calls
            match = re.search(r'logger\.(warning|error|critical)\s*\(', line)
            if match:
                log_level = match.group(1)
                results.append((i, log_level, line.strip()))

    except Exception as e:
        print(f"Error reading {file_path}: {e}", file=sys.stderr)

    return results


if __name__ == "__main__":
    # Placeholder - will add main() in next increment
    print("Analysis script - core functions defined")
