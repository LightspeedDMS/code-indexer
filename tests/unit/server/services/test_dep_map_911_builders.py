"""
Story #911 test infrastructure: anomaly/fixture builders and shared constants.

Provides factory functions used by AC1-AC6 test files.
No test methods — pure test infrastructure.

Functions:
  make_garbage_domain_anomaly  -- AnomalyEntry of type GARBAGE_DOMAIN_REJECTED
  make_source_domain_file      -- .md file with prose-fragment in outgoing table
  make_target_domain_file      -- .md file with empty incoming table
  make_domains_json_911        -- write _domains.json
  make_executor_911            -- DepMapRepairExecutor via 908 builder layer
"""

import json
from pathlib import Path
from typing import List

# ---------------------------------------------------------------------------
# Shared markdown constants
# ---------------------------------------------------------------------------

_INCOMING_TABLE_HEADER = (
    "### Incoming Dependencies\n\n"
    "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
    "|---|---|---|---|---|---|\n"
)


def _make_domain_frontmatter_header(stem: str) -> str:
    """Return minimal YAML frontmatter + Dependencies section opener for a domain file.

    Shared by make_source_domain_file and make_target_domain_file to eliminate
    copy-pasted markdown blocks.
    """
    return f"---\nname: {stem}\n---\n\n## Dependencies\n\n"


def make_garbage_domain_anomaly(filename: str, prose_fragment: str):
    """Create a real AnomalyEntry of type GARBAGE_DOMAIN_REJECTED for the given file."""
    from code_indexer.server.services.dep_map_parser_hygiene import (
        AnomalyEntry,
        AnomalyType,
    )

    return AnomalyEntry(
        type=AnomalyType.GARBAGE_DOMAIN_REJECTED,
        file=filename,
        message=f"prose-fragment target domain rejected: {prose_fragment!r}",
        channel="data",
        count=1,
    )


def make_source_domain_file(
    output_dir: Path,
    stem: str,
    prose_fragment: str,
    dep_type: str = "Service integration",
) -> Path:
    """Write domain .md file with a prose-fragment in the outgoing Target Domain cell."""
    header = _make_domain_frontmatter_header(stem)
    outgoing_section = (
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        f"| service-a | order-api | {prose_fragment} | {dep_type} | legacy | ticket-42 |\n\n"
    )
    content = header + outgoing_section + _INCOMING_TABLE_HEADER
    path = output_dir / f"{stem}.md"
    path.write_text(content, encoding="utf-8")
    return path


def make_target_domain_file(output_dir: Path, stem: str) -> Path:
    """Write target domain .md file with empty incoming table."""
    header = _make_domain_frontmatter_header(stem)
    content = header + _INCOMING_TABLE_HEADER
    path = output_dir / f"{stem}.md"
    path.write_text(content, encoding="utf-8")
    return path


def make_domains_json_911(output_dir: Path, entries: List[dict]) -> Path:
    """Write _domains.json with the given entries."""
    path = output_dir / "_domains.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def make_executor_911(**kwargs):
    """Return a DepMapRepairExecutor via the 908 builder layer."""
    from tests.unit.server.services.test_dep_map_908_builders import (
        make_executor as _make_executor_908,
    )

    return _make_executor_908(**kwargs)
