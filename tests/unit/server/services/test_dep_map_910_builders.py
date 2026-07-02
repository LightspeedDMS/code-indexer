"""
Story #910 test infrastructure: anomaly/fixture builders and shared constants.

Provides factory functions used by AC1-AC5 test files.
No test methods — pure test infrastructure.

Functions (exactly 3):
  make_malformed_yaml_anomaly  -- AnomalyEntry of type MALFORMED_YAML
  make_malformed_domain_file   -- .md file with wrong name + malformed last_analyzed
  make_domains_json            -- write _domains.json
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from code_indexer.server.services.dep_map_parser_hygiene import AnomalyEntry
    from code_indexer.server.services.dep_map_repair_executor import (
        DepMapRepairExecutor,
    )

# Re-export type names so AC test files import from the 910 builder layer
# rather than directly from production modules.
__all__ = [
    "AnomalyEntry",
    "DepMapRepairExecutor",
    "make_malformed_yaml_anomaly",
    "make_malformed_domain_file",
    "make_domains_json",
    "make_executor_910",
    "_OPENING_DELIM_LEN",
    "_CLOSING_DELIM",
    "_CLOSING_DELIM_LEN",
]

# Named constants for body-byte extraction — shared with test_dep_map_910_helpers.py.
# Opening --- occupies 3 bytes; search for closing delimiter after it.
_OPENING_DELIM_LEN: int = 3
# Closing delimiter b"\n---" is 4 bytes.
_CLOSING_DELIM: bytes = b"\n---"
_CLOSING_DELIM_LEN: int = len(_CLOSING_DELIM)


def make_malformed_yaml_anomaly(filename: str) -> "AnomalyEntry":
    """Create a real AnomalyEntry of type MALFORMED_YAML for the given file."""
    from code_indexer.server.services.dep_map_parser_hygiene import (
        AnomalyEntry,
        AnomalyType,
    )

    return AnomalyEntry(
        type=AnomalyType.MALFORMED_YAML,
        file=filename,
        message=f"malformed yaml in {filename}",
        channel="parser",
        count=1,
    )


def make_malformed_domain_file(output_dir: Path, stem: str) -> Path:
    """Write a domain .md file with deliberately wrong name and malformed last_analyzed.

    Uses 'name: wrong-name' to prove repair rewrites from _domains.json, not old value.
    Missing colon on last_analyzed line is the parse-failure trigger.
    """
    content = (
        "---\n"
        "name: wrong-name\n"
        "last_analyzed 2024-01-15T10:00:00\n"
        "participating_repos:\n"
        "  - repo-old\n"
        "---\n"
        "## Overview\n\n"
        "Some body content.\n"
    )
    path = output_dir / f"{stem}.md"
    path.write_text(content, encoding="utf-8")
    return path


def make_domains_json(output_dir: Path, entries: list) -> Path:
    """Write _domains.json with the given entries."""
    path = output_dir / "_domains.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def make_traversal_anomaly(filename: str) -> "AnomalyEntry":
    """Create a MALFORMED_YAML AnomalyEntry with a path-traversal filename.

    Used by AC3 tests to verify traversal rejection without importing
    AnomalyEntry directly from production modules.
    """
    from code_indexer.server.services.dep_map_parser_hygiene import (
        AnomalyEntry,
        AnomalyType,
    )

    return AnomalyEntry(
        type=AnomalyType.MALFORMED_YAML,
        file=filename,
        message="malformed yaml",
        channel="parser",
        count=1,
    )


def make_executor_910(**kwargs):
    """Return a DepMapRepairExecutor via the 908 builder layer.

    Thin wrapper so AC1-AC5 tests import from the 910 builder layer
    rather than directly from test_dep_map_908_builders.
    """
    from tests.unit.server.services.test_dep_map_908_builders import (
        make_executor as _make_executor_908,
    )

    return _make_executor_908(**kwargs)
