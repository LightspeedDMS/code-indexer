"""
Story #910 AC1: Field-level rewrite assertions for _repair_malformed_yaml.

Verifies that name, last_analyzed, and participating_repos are all rewritten
from _domains.json authoritative values, replacing the malformed/wrong values
in the original file.

TestAC1FieldRewrites (3 methods):
  test_name_rewritten_not_preserved_from_malformed_file
  test_last_analyzed_rewritten_with_colon_syntax
  test_repos_replaced_with_domains_json_values
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
    """Return (output_dir, executor, anomaly) for AC1 field-rewrite tests."""
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


class TestAC1FieldRewrites:
    """AC1: name, last_analyzed, repos all rewritten from _domains.json."""

    def test_name_rewritten_not_preserved_from_malformed_file(self, ac1_fixture):
        """name is rewritten to _domains.json value; old 'wrong-name' is gone."""
        output_dir, executor, anomaly = ac1_fixture

        content, errors = run_repair_and_read(output_dir, executor, anomaly, "domain-z")

        assert not errors, f"Unexpected errors: {errors}"
        assert "name: domain-z" in content
        assert "wrong-name" not in content

    def test_last_analyzed_rewritten_with_colon_syntax(self, ac1_fixture):
        """last_analyzed rewritten with proper colon from _domains.json; malformed gone."""
        output_dir, executor, anomaly = ac1_fixture

        content, errors = run_repair_and_read(output_dir, executor, anomaly, "domain-z")

        assert not errors, f"Unexpected errors: {errors}"
        assert "last_analyzed: 2024-06-01T12:00:00" in content
        assert "last_analyzed 2024-01-15T10:00:00" not in content

    def test_repos_replaced_with_domains_json_values(self, ac1_fixture):
        """participating_repos replaced by _domains.json; old repo-old is gone."""
        output_dir, executor, anomaly = ac1_fixture

        content, errors = run_repair_and_read(output_dir, executor, anomaly, "domain-z")

        assert not errors, f"Unexpected errors: {errors}"
        assert "repo-x" in content
        assert "repo-y" in content
        assert "repo-old" not in content
