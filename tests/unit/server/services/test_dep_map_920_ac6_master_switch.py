"""
Story #920 AC6: Master switch enable_graph_channel_repair interacts with per-type flags.

Master switch False + per-type enabled => master wins, no dispatch at all.
  Proven by: self-loop row still present in domain-a.md, fixed==[], return None.

Master switch True + per-type disabled => per-type gating applies, observed only.
  Proven by: disabled log message emitted (gating path exercised), self-loop row still
  present, fixed==[] (repair was suppressed not silently skipped).

Tests (exhaustive list):
  test_master_false_overrides_per_type_enabled
  test_master_true_per_type_disabled_observed_only
"""

import json
from pathlib import Path
from typing import List

from code_indexer.server.services.dep_map_health_detector import DepMapHealthDetector
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator
from code_indexer.server.services.dep_map_repair_executor import DepMapRepairExecutor

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_SELF_LOOP_ROW = "| repo-a | code | domain-a | self-ref | evidence |"
_VALID_DEP_ROW = "| repo-a | code | domain-b | valid dep | evidence |"

_SELF_LOOP_DOMAIN_A = f"""\
---
name: domain-a
participating_repos:
  - repo-a
---

## Overview

Domain domain-a.

### Outgoing Dependencies

| This Repo | Dependency Type | Target Domain | Why | Evidence |
|---|---|---|---|---|
{_SELF_LOOP_ROW}
{_VALID_DEP_ROW}

### Incoming Dependencies

| External Repo | Depends On | Source Domain | Dep Type | Why | Evidence |
|---|---|---|---|---|---|
"""

_CLEAN_DOMAIN_B = """\
---
name: domain-b
participating_repos:
  - repo-b
---

## Overview

Domain domain-b.

### Outgoing Dependencies

| This Repo | Dependency Type | Target Domain | Why | Evidence |
|---|---|---|---|---|

### Incoming Dependencies

| External Repo | Depends On | Source Domain | Dep Type | Why | Evidence |
|---|---|---|---|---|---|
"""


def _write_self_loop_fixture(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "domain-a.md").write_text(_SELF_LOOP_DOMAIN_A, encoding="utf-8")
    (output_dir / "domain-b.md").write_text(_CLEAN_DOMAIN_B, encoding="utf-8")
    domains = [
        {"name": "domain-a", "participating_repos": ["repo-a"]},
        {"name": "domain-b", "participating_repos": ["repo-b"]},
    ]
    (output_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    (output_dir / "_index.md").write_text(
        "# Index\n\n- [domain-a](domain-a.md)\n- [domain-b](domain-b.md)\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_master_false_overrides_per_type_enabled(tmp_path: Path) -> None:
    """AC6: Master switch False + per-type all=enabled => no dispatch, no file change.

    Behavioral proof that _run_phase37_repairs was never invoked:
    1. The self-loop row still exists in domain-a.md (handler would have removed it).
    2. fixed[] remains empty (no repair recorded).
    3. Return value is None (master-switch early-return path confirmed).
    """
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)

    ex = DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=False,
        graph_repair_self_loop="enabled",
        graph_repair_malformed_yaml="enabled",
        graph_repair_garbage_domain="enabled",
        graph_repair_bidirectional_mismatch="enabled",
    )
    fixed: List[str] = []
    errors: List[str] = []
    result = ex._run_phase37(output_dir, fixed, errors, dry_run=False)

    # Proof 1: self-loop row still present (dispatch bypassed)
    content_a = (output_dir / "domain-a.md").read_text(encoding="utf-8")
    assert _SELF_LOOP_ROW in content_a, (
        "AC6: self-loop row must still be present (master switch False bypasses all dispatch).\n"
        f"content_a:\n{content_a}"
    )

    # Proof 2: no fix recorded
    assert fixed == [], (
        f"AC6: fixed[] must be empty when master switch is False, got: {fixed}"
    )

    # Proof 3: early-return path (None = no DryRunReport)
    assert result is None, (
        f"AC6: master switch False must return None (early exit), got: {result!r}"
    )


def test_master_true_per_type_disabled_observed_only(tmp_path: Path) -> None:
    """AC6: Master switch True + SELF_LOOP per-type disabled => observed only, no mutation.

    Behavioral proof that the per-type gating path was exercised (not a silent skip):
    1. A log line mentioning 'SELF_LOOP' and 'disabled' is emitted — proves the gating
       code ran and observed the anomaly.
    2. The self-loop row still exists in domain-a.md — proves repair was suppressed.
    3. fixed[] is empty — repair was not silently executed.
    """
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)

    log_messages: List[str] = []
    ex = DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        graph_repair_self_loop="disabled",
        journal_callback=log_messages.append,
    )
    fixed: List[str] = []
    errors: List[str] = []
    ex._run_phase37(output_dir, fixed, errors, dry_run=False)

    # Proof 1: disabled gating log emitted (anomaly observed, path exercised)
    disabled_logs = [m for m in log_messages if "SELF_LOOP" in m and "disabled" in m]
    assert len(disabled_logs) >= 1, (
        "AC6: expected a log line with 'SELF_LOOP' and 'disabled' proving the gating "
        f"path was exercised, got log_messages: {log_messages}"
    )

    # Proof 2: self-loop row still present (repair suppressed)
    content_a = (output_dir / "domain-a.md").read_text(encoding="utf-8")
    assert _SELF_LOOP_ROW in content_a, (
        "AC6: self-loop row must remain in domain-a.md when SELF_LOOP=disabled.\n"
        f"content_a:\n{content_a}"
    )

    # Proof 3: no fix recorded
    assert fixed == [], (
        f"AC6: fixed[] must be empty when per-type=disabled, got: {fixed}"
    )
