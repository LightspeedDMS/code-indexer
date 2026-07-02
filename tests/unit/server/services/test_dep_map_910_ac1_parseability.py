"""
Story #910 AC1: Parseability assertion for _repair_malformed_yaml output.

Verifies that the repaired file's frontmatter is parseable by parse_yaml_frontmatter
(the same parser used in production) and returns exactly the authoritative values
from _domains.json (no stale, extra, or malformed entries remain).

TestAC1Parseability (1 method):
  test_repaired_frontmatter_parseable_with_authoritative_values
"""

from pathlib import Path
from typing import TYPE_CHECKING, Tuple

import pytest

from tests.unit.server.services.test_dep_map_910_builders import (
    make_domains_json,
    make_executor_910,
    make_malformed_domain_file,
    make_malformed_yaml_anomaly,
)
from tests.unit.server.services.test_dep_map_910_helpers import run_repair_and_read

if TYPE_CHECKING:
    from tests.unit.server.services.test_dep_map_910_builders import (
        AnomalyEntry,
        DepMapRepairExecutor,
    )


@pytest.fixture()
def ac1_fixture(tmp_path) -> Tuple[Path, "DepMapRepairExecutor", "AnomalyEntry"]:
    """Return (output_dir, executor, anomaly) for AC1 parseability test."""
    output_dir = tmp_path / "dependency-map"
    output_dir.mkdir(parents=True, exist_ok=True)

    domain_info = {
        "name": "domain-z",
        "last_analyzed": "2024-06-01T12:00:00",
        "participating_repos": ["repo-x", "repo-y"],
    }
    make_malformed_domain_file(output_dir, "domain-z")
    make_domains_json(output_dir, [domain_info])

    executor = make_executor_910()
    anomaly = make_malformed_yaml_anomaly("domain-z.md")
    return output_dir, executor, anomaly


class TestAC1Parseability:
    """AC1: repaired frontmatter passes parse_yaml_frontmatter with exact authoritative values."""

    def test_repaired_frontmatter_parseable_with_authoritative_values(
        self, ac1_fixture
    ):
        """parse_yaml_frontmatter returns exact name/last_analyzed/repos from _domains.json."""
        from code_indexer.server.services.dep_map_file_utils import (
            parse_yaml_frontmatter,
        )

        output_dir, executor, anomaly = ac1_fixture
        content, errors = run_repair_and_read(output_dir, executor, anomaly, "domain-z")

        assert not errors, f"Unexpected errors: {errors}"
        parsed = parse_yaml_frontmatter(content)
        assert parsed is not None, "parse_yaml_frontmatter returned None after repair"
        assert parsed.get("name") == "domain-z", f"name mismatch: {parsed}"
        assert parsed.get("last_analyzed") == "2024-06-01T12:00:00", (
            f"last_analyzed mismatch: {parsed}"
        )
        # Exact match: no stale repos (repo-old) and no extras beyond authoritative list
        assert parsed.get("participating_repos") == ["repo-x", "repo-y"], (
            f"participating_repos exact match failed: {parsed.get('participating_repos')}"
        )
