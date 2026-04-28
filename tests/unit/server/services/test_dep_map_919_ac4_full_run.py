"""
Story #919 AC4: dry_run=False (default) preserves existing mutation behavior.

Verifies:
  AC4: default dry_run=False does not change existing Stories 1-5 behavior
  AC4: _run_phase37 called without dry_run keyword still mutates (backward compat)
  AC4: enable_graph_channel_repair=False no-ops regardless of dry_run value

Tests (exhaustive list):
  test_default_dry_run_false_removes_self_loop
  test_positional_call_without_dry_run_removes_self_loop
  test_enable_flag_false_skips_all_repair_dry_run_or_not

Module-level helpers (exhaustive list):
  _make_executor(enable)              -- DepMapRepairExecutor with real deps
  _write_self_loop_fixture(tmp_path)  -- dep-map dir with SELF_LOOP in domain-a
  _self_loop_still_present(md_path)   -- True if domain-a.md still has self-loop row
"""

import json
from pathlib import Path
from typing import List

from code_indexer.server.services.dep_map_repair_executor import DepMapRepairExecutor
from code_indexer.server.services.dep_map_health_detector import DepMapHealthDetector
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_executor(enable: bool = True) -> DepMapRepairExecutor:
    """Build a DepMapRepairExecutor with real deps and no Claude fn.

    Story #920: graph_repair_self_loop='enabled' so mutation tests exercise the
    real write path. Master flag=False cases are unaffected (early exit wins).
    """
    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=enable,
        invoke_claude_fn=None,
        graph_repair_self_loop="enabled",
    )


_SELF_LOOP_MD = """\
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
| repo-a | code | domain-a | self-ref | evidence |
| repo-a | code | domain-b | valid dep | evidence |

### Incoming Dependencies

| External Repo | Depends On | Source Domain | Dep Type | Why | Evidence |
|---|---|---|---|---|---|
"""

_CLEAN_B_MD = """\
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


def _write_self_loop_fixture(output_dir: Path) -> Path:
    """Write a dep-map directory with a SELF_LOOP anomaly in domain-a. Returns output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "domain-a.md").write_text(_SELF_LOOP_MD, encoding="utf-8")
    (output_dir / "domain-b.md").write_text(_CLEAN_B_MD, encoding="utf-8")
    domains = [
        {"name": "domain-a", "participating_repos": ["repo-a"]},
        {"name": "domain-b", "participating_repos": ["repo-b"]},
    ]
    (output_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    (output_dir / "_index.md").write_text("# Index\n", encoding="utf-8")
    return output_dir


def _self_loop_still_present(md_path: Path) -> bool:
    """Return True if domain-a.md still contains the self-referencing outgoing row."""
    content = md_path.read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.startswith("| repo-a |") and "| domain-a |" in line:
            return True
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_default_dry_run_false_removes_self_loop(tmp_path: Path) -> None:
    """AC4: explicit dry_run=False removes self-loop row (existing behavior unchanged)."""
    output_dir = _write_self_loop_fixture(tmp_path / "dependency-map")

    executor = _make_executor()
    fixed: List[str] = []
    errors: List[str] = []
    executor._run_phase37(output_dir, fixed, errors, dry_run=False)

    assert not _self_loop_still_present(output_dir / "domain-a.md"), (
        "Self-loop row still present after normal run"
    )
    assert any("self-loop" in f for f in fixed), f"Expected fix entry, got: {fixed}"


def test_positional_call_without_dry_run_removes_self_loop(tmp_path: Path) -> None:
    """AC4: calling _run_phase37(output_dir, fixed, errors) without dry_run still mutates."""
    output_dir = _write_self_loop_fixture(tmp_path / "dependency-map")

    executor = _make_executor()
    fixed: List[str] = []
    errors: List[str] = []
    # Call without dry_run keyword — must default to False (backward compat)
    executor._run_phase37(output_dir, fixed, errors)

    assert not _self_loop_still_present(output_dir / "domain-a.md"), (
        "Self-loop row still present after positional call without dry_run"
    )


def test_enable_flag_false_skips_all_repair_dry_run_or_not(tmp_path: Path) -> None:
    """AC4: enable_graph_channel_repair=False no-ops both dry_run=True and dry_run=False."""
    output_dir = _write_self_loop_fixture(tmp_path / "dependency-map")
    original_content = (output_dir / "domain-a.md").read_text(encoding="utf-8")

    executor = _make_executor(enable=False)
    fixed_dry: List[str] = []
    errors_dry: List[str] = []
    executor._run_phase37(output_dir, fixed_dry, errors_dry, dry_run=True)

    fixed_normal: List[str] = []
    errors_normal: List[str] = []
    executor._run_phase37(output_dir, fixed_normal, errors_normal, dry_run=False)

    # Both runs produce no changes when flag is disabled
    assert (output_dir / "domain-a.md").read_text(encoding="utf-8") == original_content
    assert not fixed_dry and not fixed_normal
