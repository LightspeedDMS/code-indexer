"""
Unit tests for Story #876 Phase B-2 Deliverable 1.

Verifies the HealthReport dataclass gains a ``lifecycle: list[str]`` field
alongside the existing dep-map anomaly list, and that the field propagates
through ``to_dict()`` and ``needs_repair`` property semantics.

MOCK BOUNDARY MAP:
  REAL:   HealthReport dataclass (from dep_map_health_detector module).
  MOCKED: None — pure dataclass behaviour, no external I/O.

Test discipline:
  - Each test ≤30 lines.
  - No patches; real dataclass instances only.
  - Precise assertions on the new lifecycle contract and backward-compat of
    existing to_dict() top-level keys.
"""

from code_indexer.server.services.dep_map_health_detector import HealthReport


def test_health_report_lifecycle_field_defaults_to_empty_list():
    """New HealthReport with no lifecycle argument has empty list, not None."""
    report = HealthReport(status="healthy")

    assert report.lifecycle == []
    assert isinstance(report.lifecycle, list)


def test_health_report_lifecycle_field_accepts_aliases():
    """HealthReport constructor accepts lifecycle list of alias strings."""
    report = HealthReport(status="healthy", lifecycle=["repo-a", "repo-b"])

    assert report.lifecycle == ["repo-a", "repo-b"]


def test_health_report_needs_repair_false_when_healthy_and_lifecycle_empty():
    """Healthy status + empty lifecycle → needs_repair is False (new contract)."""
    report = HealthReport(status="healthy", lifecycle=[])

    assert report.needs_repair is False


def test_health_report_needs_repair_true_when_lifecycle_non_empty():
    """Healthy status but lifecycle non-empty → needs_repair is True (new contract)."""
    report = HealthReport(status="healthy", lifecycle=["broken-repo"])

    assert report.needs_repair is True


def test_health_report_to_dict_includes_empty_lifecycle():
    """to_dict() always emits the lifecycle key, even when empty."""
    report = HealthReport(status="healthy")

    result = report.to_dict()

    assert result["lifecycle"] == []


def test_health_report_to_dict_includes_lifecycle_aliases():
    """to_dict() includes the full lifecycle alias list."""
    report = HealthReport(status="healthy", lifecycle=["repo-a", "repo-b"])

    result = report.to_dict()

    assert result["lifecycle"] == ["repo-a", "repo-b"]


def test_health_report_to_dict_preserves_existing_keys_with_lifecycle():
    """Adding lifecycle field does not break existing to_dict() top-level keys."""
    report = HealthReport(status="healthy", lifecycle=["alias-x"])

    result = report.to_dict()

    assert "status" in result
    assert "anomalies" in result
    assert "repairable_count" in result
    assert "output_dir" in result
    assert "lifecycle" in result
