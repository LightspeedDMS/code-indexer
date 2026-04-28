"""
Story #910 AC2: Body unrecoverable — routes to Phase 1 full re-analysis.

When _locate_frontmatter_bounds returns None (no closing --- found),
_repair_malformed_yaml must not attempt surgical re-emit. Instead it routes
to the existing _domain_analyzer callback (Phase 1 equivalent).

TestAC2Phase1Fallback (3 methods):
  test_domain_analyzer_called_once_when_bounds_none
  test_errors_when_domain_analyzer_is_none
  test_errors_when_domain_analyzer_raises
"""

from pathlib import Path
from typing import TYPE_CHECKING, List

import pytest

from tests.unit.server.services.test_dep_map_910_builders import (
    make_domains_json,
    make_executor_910,
    make_malformed_yaml_anomaly,
)

if TYPE_CHECKING:
    pass


def make_no_closing_delimiter_file(output_dir: Path, stem: str) -> Path:
    """Write a domain .md file with opening --- but no closing --- delimiter."""
    content = (
        "---\n"
        f"name: {stem}\n"
        "last_analyzed: 2024-01-15T10:00:00\n"
        "This file has no closing --- delimiter so frontmatter bounds cannot be located.\n"
        "Some body content that is all mixed up.\n"
    )
    path = output_dir / f"{stem}.md"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture()
def ac2_base_fixture(tmp_path):
    """Return (output_dir, anomaly, domain_info) for AC2 tests."""
    output_dir = tmp_path / "dependency-map"
    output_dir.mkdir(parents=True, exist_ok=True)

    domain_info = {
        "name": "broken-domain",
        "last_analyzed": "2024-06-01T12:00:00",
        "participating_repos": ["repo-a"],
    }
    make_no_closing_delimiter_file(output_dir, "broken-domain")
    make_domains_json(output_dir, [domain_info])
    anomaly = make_malformed_yaml_anomaly("broken-domain.md")
    return output_dir, anomaly, domain_info


class TestAC2Phase1Fallback:
    """AC2: No recoverable frontmatter bounds — routes to _domain_analyzer."""

    def test_domain_analyzer_called_once_when_bounds_none(self, ac2_base_fixture):
        """_domain_analyzer is called exactly once when frontmatter bounds are missing."""
        output_dir, anomaly, domain_info = ac2_base_fixture

        call_count = [0]

        def counting_analyzer(out_dir, d_info, d_list, repo_list):
            call_count[0] += 1

        from code_indexer.server.services.dep_map_health_detector import (
            DepMapHealthDetector,
        )
        from code_indexer.server.services.dep_map_index_regenerator import (
            IndexRegenerator,
        )
        from code_indexer.server.services.dep_map_repair_executor import (
            DepMapRepairExecutor,
        )

        executor = DepMapRepairExecutor(
            health_detector=DepMapHealthDetector(),
            index_regenerator=IndexRegenerator(),
            domain_analyzer=counting_analyzer,
        )
        fixed: List[str] = []
        errors: List[str] = []

        executor._repair_malformed_yaml(output_dir, anomaly, fixed, errors)

        assert call_count[0] == 1, (
            f"Expected _domain_analyzer called exactly once, got: {call_count[0]}"
        )

    def test_errors_when_domain_analyzer_is_none(self, ac2_base_fixture):
        """errors[] receives 'needs full re-analysis but no domain_analyzer wired'."""
        output_dir, anomaly, _ = ac2_base_fixture
        executor = make_executor_910()  # no domain_analyzer wired
        fixed: List[str] = []
        errors: List[str] = []

        executor._repair_malformed_yaml(output_dir, anomaly, fixed, errors)

        assert any(
            "needs full re-analysis but no domain_analyzer wired" in e for e in errors
        ), f"Expected no-analyzer error, got: {errors}"
        assert fixed == [], f"Expected empty fixed, got: {fixed}"

    def test_fixed_not_appended_when_analyzer_returns_falsy(self, ac2_base_fixture):
        """fixed[] stays empty when _domain_analyzer returns None (falsy).

        The fallback must only record success when the analyzer returns a truthy
        value, consistent with the Phase 1 pattern in _run_phase1.
        """
        output_dir, anomaly, _ = ac2_base_fixture

        def falsy_analyzer(out_dir, d_info, d_list, repo_list):
            return None  # falsy — analysis produced no result

        from code_indexer.server.services.dep_map_health_detector import (
            DepMapHealthDetector,
        )
        from code_indexer.server.services.dep_map_index_regenerator import (
            IndexRegenerator,
        )
        from code_indexer.server.services.dep_map_repair_executor import (
            DepMapRepairExecutor,
        )

        executor = DepMapRepairExecutor(
            health_detector=DepMapHealthDetector(),
            index_regenerator=IndexRegenerator(),
            domain_analyzer=falsy_analyzer,
        )
        fixed: List[str] = []
        errors: List[str] = []

        executor._repair_malformed_yaml(output_dir, anomaly, fixed, errors)

        assert fixed == [], (
            f"fixed[] must be empty when _domain_analyzer returns falsy, got: {fixed}"
        )

    def test_errors_when_domain_analyzer_raises(self, ac2_base_fixture):
        """errors[] receives 'full re-analysis failed' when _domain_analyzer raises."""
        output_dir, anomaly, _ = ac2_base_fixture

        def failing_analyzer(out_dir, d_info, d_list, repo_list):
            raise RuntimeError("Claude CLI timeout")

        from code_indexer.server.services.dep_map_health_detector import (
            DepMapHealthDetector,
        )
        from code_indexer.server.services.dep_map_index_regenerator import (
            IndexRegenerator,
        )
        from code_indexer.server.services.dep_map_repair_executor import (
            DepMapRepairExecutor,
        )

        executor = DepMapRepairExecutor(
            health_detector=DepMapHealthDetector(),
            index_regenerator=IndexRegenerator(),
            domain_analyzer=failing_analyzer,
        )
        fixed: List[str] = []
        errors: List[str] = []

        executor._repair_malformed_yaml(output_dir, anomaly, fixed, errors)

        assert any("full re-analysis failed" in e for e in errors), (
            f"Expected re-analysis-failed error, got: {errors}"
        )
        assert fixed == [], f"Expected empty fixed, got: {fixed}"
