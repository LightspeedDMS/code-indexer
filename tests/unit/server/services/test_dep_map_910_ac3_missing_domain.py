"""
Story #910 AC3: Domain missing from _domains.json — no re-emit attempted.

When the malformed file's stem has no matching entry in _domains.json, the handler
must record a precise error and leave the file unchanged on disk.

TestAC3MissingDomain (3 methods):
  test_errors_when_domain_not_in_domains_json  -- exact error msg + file bytes unchanged
  test_errors_when_md_file_not_found           -- exact error msg + file still absent
  test_errors_when_path_traversal_in_filename  -- exact rejection msg + no new files
"""

from typing import List

from tests.unit.server.services.test_dep_map_910_builders import (
    make_domains_json,
    make_executor_910,
    make_malformed_domain_file,
    make_malformed_yaml_anomaly,
    make_traversal_anomaly,
)


class TestAC3MissingDomain:
    """AC3: Domain-not-found and path-safety error paths; file always left unchanged."""

    def test_errors_when_domain_not_in_domains_json(self, tmp_path):
        """errors[] gets missing-domain message; file bytes unchanged after repair attempt."""
        output_dir = tmp_path / "dependency-map"
        output_dir.mkdir(parents=True, exist_ok=True)
        md_path = make_malformed_domain_file(output_dir, "untracked-domain")
        make_domains_json(
            output_dir, [{"name": "other-domain", "participating_repos": []}]
        )
        original_bytes = md_path.read_bytes()

        executor = make_executor_910()
        anomaly = make_malformed_yaml_anomaly("untracked-domain.md")
        fixed: List[str] = []
        errors: List[str] = []

        executor._repair_malformed_yaml(output_dir, anomaly, fixed, errors)

        assert any(
            "cannot re-emit frontmatter" in e and "not in _domains.json" in e
            for e in errors
        ), f"Expected missing-domain error, got: {errors}"
        assert fixed == [], f"Expected no fixed entries, got: {fixed}"
        assert md_path.read_bytes() == original_bytes, "File was modified despite error"

    def test_errors_when_md_file_not_found(self, tmp_path):
        """errors[] gets 'file not found'; the absent .md file still does not exist after."""
        output_dir = tmp_path / "dependency-map"
        output_dir.mkdir(parents=True, exist_ok=True)
        make_domains_json(
            output_dir, [{"name": "ghost-domain", "participating_repos": []}]
        )
        ghost_path = output_dir / "ghost-domain.md"

        executor = make_executor_910()
        anomaly = make_malformed_yaml_anomaly("ghost-domain.md")
        fixed: List[str] = []
        errors: List[str] = []

        executor._repair_malformed_yaml(output_dir, anomaly, fixed, errors)

        assert any("file not found" in e for e in errors), (
            f"Expected file-not-found error, got: {errors}"
        )
        assert fixed == [], f"Expected no fixed entries, got: {fixed}"
        assert not ghost_path.exists(), "ghost-domain.md was created despite error"

    def test_errors_when_path_traversal_in_filename(self, tmp_path):
        """errors[] gets exact rejection msg; no new files created in output_dir."""
        output_dir = tmp_path / "dependency-map"
        output_dir.mkdir(parents=True, exist_ok=True)
        make_domains_json(output_dir, [])
        files_before = set(output_dir.rglob("*"))

        executor = make_executor_910()
        anomaly = make_traversal_anomaly("../etc/passwd.md")
        fixed: List[str] = []
        errors: List[str] = []

        executor._repair_malformed_yaml(output_dir, anomaly, fixed, errors)

        assert any(
            "rejected unsafe path in malformed-yaml anomaly" in e for e in errors
        ), f"Expected exact path-safety rejection message, got: {errors}"
        assert fixed == [], f"Expected no fixed entries, got: {fixed}"
        files_after = set(output_dir.rglob("*"))
        assert files_after == files_before, (
            f"New files created after traversal rejection: {files_after - files_before}"
        )
