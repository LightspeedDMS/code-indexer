"""
Story #920 AC2: Per-type "disabled" flag for SELF_LOOP skips handler, logs, and skips.

Tests (exhaustive list):
  test_disabled_type_does_not_call_handler    -- SELF_LOOP disabled => no file mutation
  test_disabled_type_emits_log_line           -- disabled emits log with "SELF_LOOP" + "disabled"
  test_disabled_type_appears_in_skipped_under_invocation_dry_run
                                              -- report.skipped contains ("self_loop", "type_disabled_by_config")
  test_disabled_type_does_not_journal         -- disabled type produces no journal entry
"""

import hashlib
import json
from pathlib import Path
from typing import Dict, List

import pytest

from code_indexer.server.services.dep_map_health_detector import DepMapHealthDetector
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator
from code_indexer.server.services.dep_map_repair_executor import DepMapRepairExecutor

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_SELF_LOOP_DOMAIN_A = """\
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

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _write_self_loop_fixture(output_dir: Path) -> None:
    """Write dep-map with SELF_LOOP anomaly in domain-a."""
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


def _sha256_all_files(directory: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            result[str(p.relative_to(directory))] = digest
    return result


def _make_disabled_executor(journal_callback=None) -> DepMapRepairExecutor:
    """Build executor with graph_repair_self_loop='disabled' and real deps."""
    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        graph_repair_self_loop="disabled",
        journal_callback=journal_callback,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_disabled_type_does_not_call_handler(tmp_path: Path) -> None:
    """AC2: graph_repair_self_loop='disabled' => SELF_LOOP handler is never invoked.

    Verified by sha256 of all dep-map files remaining identical — the self-loop
    row in domain-a.md would be deleted by the handler if it were called.
    """
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)
    before = _sha256_all_files(output_dir)

    ex = _make_disabled_executor()
    ex._run_phase37(output_dir, [], [], dry_run=False)

    after = _sha256_all_files(output_dir)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert not changed, (
        f"AC2: files changed despite graph_repair_self_loop=disabled: {sorted(changed)}"
    )


def test_disabled_type_emits_log_line(tmp_path: Path) -> None:
    """AC2: graph_repair_self_loop='disabled' emits a log line mentioning SELF_LOOP and disabled."""
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)

    log_messages: List[str] = []
    ex = _make_disabled_executor(journal_callback=log_messages.append)
    ex._run_phase37(output_dir, [], [], dry_run=False)

    disabled_logs = [m for m in log_messages if "SELF_LOOP" in m and "disabled" in m]
    assert len(disabled_logs) >= 1, (
        f"AC2: expected log line with 'SELF_LOOP' and 'disabled', got: {log_messages}"
    )


def test_disabled_type_appears_in_skipped_under_invocation_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: under invocation dry_run=True, disabled self_loop appears as
    ("self_loop", "type_disabled_by_config") in report.skipped."""
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)

    ex = _make_disabled_executor()
    report = ex._run_phase37(output_dir, [], [], dry_run=True)

    assert report is not None, "dry_run=True must return a DryRunReport"
    assert ("self_loop", "type_disabled_by_config") in report.skipped, (
        f"AC2: expected ('self_loop', 'type_disabled_by_config') in report.skipped, "
        f"got: {report.skipped}"
    )


def test_disabled_type_does_not_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: disabled type produces no journal entry."""
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)

    ex = _make_disabled_executor()
    ex._run_phase37(output_dir, [], [], dry_run=False)

    journal_path = tmp_path / "cidx_data" / "dep_map_repair_journal.jsonl"
    if journal_path.exists():
        content = journal_path.read_text().strip()
        assert content == "", (
            f"AC2: journal must be empty for disabled type, but found: {content!r}"
        )
