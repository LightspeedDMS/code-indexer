#!/usr/bin/env python3
"""Standalone glob file matching script for subprocess-based file discovery.

This script performs glob pattern matching on filesystem paths and returns
results as JSON. It's designed to be called via subprocess with timeout and
process isolation protections.

Input: JSON config file path as first argument:
    {
        "search_path": "/path/to/search",
        "include_patterns": ["**/*.py", "code/**/Main.java"],
        "exclude_patterns": ["test_*", "*.tmp"]
    }

Output: JSON array of relative file paths to stdout:
    ["path/to/file1.py", "path/to/file2.py", ...]

Exit codes:
    0: Success (even if no matches found)
    1: Error (invalid config, path doesn't exist, etc.)

All matching routes through the shared PathPatternMatcher so grep fallback
selection has the same normalization, brace expansion, and gitwildmatch
semantics as indexed regex and X-Ray searches.
"""

import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# This script executes as a standalone child process, so add the project's
# source tree before importing the shared application-layer matcher.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from code_indexer.services.path_pattern_matcher import PathPatternMatcher  # noqa: E402


def glob_files(
    search_path: Path,
    include_patterns: List[str],
    exclude_patterns: Optional[List[str]],
    match_prefix: str = "",
) -> List[str]:
    """Find files selected by the shared, compiled path-pattern matcher.

    Args:
        search_path: Base directory to WALK from. Returned paths are
            relative to this directory (unchanged by ``match_prefix``).
        include_patterns: List of glob patterns following the canonical
            policy -- REPO-relative, matching every other engine
            (indexed matcher, Python multiline walk, real ripgrep ``-g``).
        exclude_patterns: Optional list of patterns to exclude from results.
        match_prefix: Bug #1876 round-5 finding F4. The repository-
            relative path prefix of ``search_path`` itself (e.g. "src"
            when ``search_path`` is ``<repo>/src``), or "" when
            ``search_path`` already IS the repository root. Every
            pattern is matched against ``match_prefix + "/" +
            relative_path`` (or just ``relative_path`` when the prefix
            is empty) instead of ``relative_path`` alone -- a caller
            that narrows ``search_path`` below the repo root (the
            MCP/REST ``path`` parameter) must still see include/exclude
            patterns interpreted relative to the repo root, exactly like
            ripgrep's own ``-g`` flag and the indexed matcher, not
            relative to the narrowed walk root.

    Returns:
        List of relative file paths (relative to ``search_path``, NOT
        prefixed) as strings for all files matching include patterns and
        not matching exclude patterns. Empty list if no matches found.

    Raises:
        ValueError: If search_path doesn't exist or isn't a directory.
    """
    if not search_path.exists():
        raise ValueError(f"Search path does not exist: {search_path}")
    if not search_path.is_dir():
        raise ValueError(f"Search path is not a directory: {search_path}")

    selector = PathPatternMatcher().create_selector(include_patterns, exclude_patterns)
    matched_files: List[str] = []
    for file_path in search_path.rglob("*"):
        if not file_path.is_file():
            continue
        relative_path = file_path.relative_to(search_path).as_posix()
        match_path = (
            f"{match_prefix}/{relative_path}" if match_prefix else relative_path
        )
        if selector.select(match_path):
            matched_files.append(relative_path)

    return sorted(matched_files)


def parse_and_validate_config(
    config_file: str,
) -> Tuple[Path, List[str], Optional[List[str]], str]:
    """Parse and validate config file.

    Args:
        config_file: Path to JSON config file

    Returns:
        Tuple of (search_path, include_patterns, exclude_patterns,
        match_prefix)

    Raises:
        FileNotFoundError: If config file doesn't exist
        json.JSONDecodeError: If config file contains invalid JSON
        ValueError: If config is missing required fields or has invalid types
    """
    with open(config_file, "r") as f:
        config = json.load(f)

    # Validate required fields
    if "search_path" not in config:
        raise ValueError("Config missing required field: search_path")
    if "include_patterns" not in config:
        raise ValueError("Config missing required field: include_patterns")

    # Extract and validate types
    search_path = Path(config["search_path"])
    include_patterns = config["include_patterns"]
    exclude_patterns = config.get("exclude_patterns")
    match_prefix = config.get("match_prefix", "")

    if not isinstance(include_patterns, list):
        raise ValueError("include_patterns must be a list")
    if exclude_patterns is not None and not isinstance(exclude_patterns, list):
        raise ValueError("exclude_patterns must be a list or null")
    if not isinstance(match_prefix, str):
        raise ValueError("match_prefix must be a string")

    return search_path, include_patterns, exclude_patterns, match_prefix


def main() -> int:
    """Main entry point for subprocess execution.

    Reads JSON config from file specified as first argument, performs glob matching,
    and outputs JSON array of file paths to stdout.

    Returns:
        Exit code (0 for success, 1 for error)
    """
    try:
        # Expect config file path as first argument
        if len(sys.argv) != 2:
            print(json.dumps([]))
            sys.stderr.write("Usage: glob_files.py <config_file_path>\n")
            return 1

        config_file = sys.argv[1]

        # Parse and validate config
        (
            search_path,
            include_patterns,
            exclude_patterns,
            match_prefix,
        ) = parse_and_validate_config(config_file)

        # Perform glob matching
        files = glob_files(
            search_path, include_patterns, exclude_patterns, match_prefix
        )

        # Output results as JSON array
        print(json.dumps(files))
        return 0

    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        # Known error types - return empty array and write error to stderr
        print(json.dumps([]))
        sys.stderr.write(f"Error: {e}\n")
        return 1

    except Exception as e:
        # Unexpected errors - return empty array and write error to stderr
        print(json.dumps([]))
        sys.stderr.write(f"Unexpected error: {type(e).__name__}: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
