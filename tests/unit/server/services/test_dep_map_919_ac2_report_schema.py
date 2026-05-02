"""
Story #919 AC2/AC6: DryRunReport schema tests.

Verifies:
  AC2: JSON report has exactly these keys: mode, timestamp, total_anomalies,
       per_type_counts, per_verdict_counts, per_action_counts,
       would_be_writes, skipped
  AC6: json.dumps(asdict(report), default=str) produces valid JSON parseable by json.loads
  would_be_writes is list[tuple[str, str]] — verified via isinstance on container+element
  skipped is list[tuple[str, str]] — verified via isinstance on container+element
  mode is always "dry_run"

Tests (exhaustive list):
  test_report_has_required_keys
  test_report_mode_is_dry_run
  test_report_json_round_trips
  test_would_be_writes_is_list_of_str_tuples
  test_skipped_is_list_of_str_tuples
"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import List, Tuple

from code_indexer.server.services.dep_map_repair_executor import DryRunReport

_REQUIRED_KEYS = {
    "mode",
    "timestamp",
    "total_anomalies",
    "per_type_counts",
    "per_verdict_counts",
    "per_action_counts",
    "would_be_writes",
    "skipped",
    "errors",
}


def _make_minimal_report() -> DryRunReport:
    """Build a minimal DryRunReport with empty collections."""
    return DryRunReport(
        mode="dry_run",
        timestamp="2026-01-01T00:00:00+00:00",
        total_anomalies=0,
        per_type_counts={},
        per_verdict_counts={},
        per_action_counts={},
        would_be_writes=[],
        skipped=[],
        errors=[],
    )


def test_report_has_required_keys() -> None:
    """AC2: asdict(report) contains exactly the required keys."""
    report = _make_minimal_report()
    keys = set(asdict(report).keys())
    assert keys == _REQUIRED_KEYS


def test_report_mode_is_dry_run() -> None:
    """AC2: mode field is always 'dry_run'."""
    report = _make_minimal_report()
    assert report.mode == "dry_run"


def test_report_json_round_trips() -> None:
    """AC6: json.dumps(asdict(report), default=str) round-trips cleanly."""
    report = DryRunReport(
        mode="dry_run",
        timestamp="2026-01-01T00:00:00+00:00",
        total_anomalies=3,
        per_type_counts={"SELF_LOOP": 2, "MALFORMED_YAML": 1},
        per_verdict_counts={"NA": 3},
        per_action_counts={"self_loop_deleted": 2, "malformed_yaml_reemitted": 1},
        would_be_writes=[
            ("domain-a.md", "row_deleted"),
            ("domain-b.md", "frontmatter_reemitted"),
        ],
        skipped=[("BIDIRECTIONAL_MISMATCH", "no_invoke_llm_fn")],
        errors=[],
    )
    serialized = json.dumps(asdict(report), default=str)
    parsed = json.loads(serialized)
    assert parsed["mode"] == "dry_run"
    assert parsed["total_anomalies"] == 3
    assert set(parsed.keys()) == _REQUIRED_KEYS


def test_would_be_writes_is_list_of_str_tuples() -> None:
    """AC2: would_be_writes is list[tuple[str, str]] — container and element isinstance checks."""
    report = DryRunReport(
        mode="dry_run",
        timestamp="t",
        total_anomalies=1,
        per_type_counts={},
        per_verdict_counts={},
        per_action_counts={},
        would_be_writes=[("some_file.md", "row_deleted")],
        skipped=[],
        errors=[],
    )
    assert isinstance(report.would_be_writes, list)
    assert len(report.would_be_writes) == 1
    item = report.would_be_writes[0]
    assert isinstance(item, tuple)
    assert len(item) == 2
    assert all(isinstance(x, str) for x in item)


def test_skipped_is_list_of_str_tuples() -> None:
    """AC2: skipped is list[tuple[str, str]] — container and element isinstance checks."""
    report = DryRunReport(
        mode="dry_run",
        timestamp="t",
        total_anomalies=0,
        per_type_counts={},
        per_verdict_counts={},
        per_action_counts={},
        would_be_writes=[],
        skipped=[("BIDIRECTIONAL_MISMATCH", "no_invoke_llm_fn")],
        errors=[],
    )
    assert isinstance(report.skipped, list)
    assert len(report.skipped) == 1
    item = report.skipped[0]
    assert isinstance(item, tuple)
    assert len(item) == 2
    assert all(isinstance(x, str) for x in item)


def test_verdict_counts_use_NA_not_underscore() -> None:
    """Blocker 5: _tally_dry_run_actions must emit 'NA' (no underscore), not 'N_A'.

    Regression guard for schema vocabulary drift identified in Codex cycle-2 review.
    """
    from code_indexer.server.services.dep_map_repair_executor import (
        DepMapRepairExecutor,
    )

    entries = [
        "Phase 3.7: deleted self-loop in domain-a",
        "Phase 3.7: reemitted malformed yaml in domain-b",
    ]
    _per_action, per_verdict = DepMapRepairExecutor._tally_dry_run_actions(entries)
    assert "N_A" not in per_verdict, (
        f"per_verdict must not use 'N_A' (underscore), got: {per_verdict}"
    )
    assert "NA" in per_verdict, (
        f"per_verdict must use 'NA' (no underscore), got: {per_verdict}"
    )


def test_would_be_writes_operation_label_is_remapped_outgoing_row(
    tmp_path,
) -> None:
    """Blocker 5: GARBAGE_DOMAIN_REJECTED dry-run would-be write label must be 'remapped_outgoing_row'.

    Regression guard for schema vocabulary drift: the story documents
    'remapped_outgoing_row' as the GARBAGE_DOMAIN_REJECTED operation label.
    """
    import json
    from code_indexer.server.services.dep_map_repair_executor import (
        DepMapRepairExecutor,
    )
    from code_indexer.server.services.dep_map_health_detector import (
        DepMapHealthDetector,
    )
    from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator

    output_dir = tmp_path / "dependency-map"
    output_dir.mkdir()
    domains = [
        {"name": "domain-a", "participating_repos": ["repo-a"]},
        {"name": "domain-b", "participating_repos": ["repo-b"]},
    ]
    (output_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    (output_dir / "_index.md").write_text("# index", encoding="utf-8")
    # outgoing row with prose-fragment target (parens) whose message contains 'repo-b'
    (output_dir / "domain-a.md").write_text(
        "---\nname: domain-a\nparticipating_repos:\n  - repo-a\n---\n"
        "### Outgoing Dependencies\n"
        "| This Repo | Dependency Type | Target Domain | Why | Evidence |\n"
        "|---|---|---|---|---|\n"
        "| repo-a | code | depends on repo-b (internal) | why | evidence |\n"
        "### Incoming Dependencies\n"
        "| External Repo | Depends On | Source Domain | Dep Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n",
        encoding="utf-8",
    )
    (output_dir / "domain-b.md").write_text(
        "---\nname: domain-b\nparticipating_repos:\n  - repo-b\n---\n"
        "### Outgoing Dependencies\n"
        "| This Repo | Dependency Type | Target Domain | Why | Evidence |\n"
        "|---|---|---|---|---|\n"
        "### Incoming Dependencies\n"
        "| External Repo | Depends On | Source Domain | Dep Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n",
        encoding="utf-8",
    )

    executor = DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        invoke_llm_fn=None,
    )
    fixed: List[str] = []
    errors: List[str] = []
    would_be_writes: List[Tuple[Path, str]] = []
    executor._run_phase37_repairs(
        output_dir, fixed, errors, dry_run=True, would_be_writes=would_be_writes
    )

    labels = [op for _path, op in would_be_writes]
    assert "remapped_outgoing_row" in labels, (
        f"Expected 'remapped_outgoing_row' in would_be_writes labels, got: {labels}"
    )
