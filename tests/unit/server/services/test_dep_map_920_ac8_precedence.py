"""
Story #920 AC8: Precedence between per-type flags and invocation-level dry_run.

Three cases:
  per_type=enabled + invocation_dry_run=True => no writes (invocation overrides)
  per_type=disabled + invocation_dry_run=True => disabled (no handler; skipped entry
      recorded in dry-run report as 'type_disabled_by_config')
  per_type=dry_run + invocation_dry_run=False => still dry_run (per-type wins, no write)

Tests (exhaustive list):
  test_per_type_enabled_invocation_dry_run_true_no_writes
  test_per_type_disabled_invocation_dry_run_true_skipped_in_report
  test_per_type_dry_run_invocation_false_still_no_write
"""

import hashlib
import json
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.services.dep_map_health_detector import DepMapHealthDetector
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator
from code_indexer.server.services.dep_map_repair_executor import DepMapRepairExecutor

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_SELF_LOOP_ROW = "| repo-a | code | domain-a | self-ref | evidence |"

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


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_per_type_enabled_invocation_dry_run_true_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC8: per_type=enabled + invocation dry_run=True => no writes (invocation overrides).

    is_effective_dry_run(True, 'enabled') == True, so the handler runs in dry-run
    mode and must not mutate domain-a.md even though per-type flag is 'enabled'.
    The self-loop row must still be present.
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)
    before_digest = _sha256_file(output_dir / "domain-a.md")

    ex = DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        graph_repair_self_loop="enabled",
    )
    fixed: List[str] = []
    errors: List[str] = []
    # invocation dry_run=True overrides per-type enabled
    ex._run_phase37(output_dir, fixed, errors, dry_run=True)

    after_digest = _sha256_file(output_dir / "domain-a.md")
    assert before_digest == after_digest, (
        "AC8: invocation dry_run=True must prevent writes even when per-type=enabled"
    )
    content_a = (output_dir / "domain-a.md").read_text(encoding="utf-8")
    assert _SELF_LOOP_ROW in content_a, (
        "AC8: self-loop row must still exist (invocation dry_run=True prevented removal)"
    )


def test_per_type_disabled_invocation_dry_run_true_skipped_in_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC8: per_type=disabled + invocation dry_run=True => disabled (no handler;
    skipped entry recorded in dry-run report as 'type_disabled_by_config').

    The disabled path is taken (not the dry-run path): no handler is invoked, and
    the anomaly is recorded in report.skipped as ('self_loop', 'type_disabled_by_config').
    The self-loop row remains in domain-a.md (no repair occurred).
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)

    ex = DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        graph_repair_self_loop="disabled",
    )
    fixed: List[str] = []
    errors: List[str] = []
    # invocation dry_run=True + per-type=disabled => disabled path, not dry-run path
    report = ex._run_phase37(output_dir, fixed, errors, dry_run=True)

    # self-loop row still present (disabled = no handler called)
    content_a = (output_dir / "domain-a.md").read_text(encoding="utf-8")
    assert _SELF_LOOP_ROW in content_a, (
        "AC8: self-loop row must remain when per-type=disabled (handler not called)"
    )
    # report.skipped must contain the disabled entry proving disabled path was taken
    assert report is not None, "dry_run=True must return a DryRunReport"
    assert ("self_loop", "type_disabled_by_config") in report.skipped, (
        f"AC8: disabled type must appear in report.skipped as ('self_loop', 'type_disabled_by_config'), "
        f"got: {report.skipped}"
    )


def test_per_type_dry_run_invocation_false_still_no_write(tmp_path: Path) -> None:
    """AC8: per_type=dry_run + invocation dry_run=False => still no write (per-type wins).

    is_effective_dry_run(False, 'dry_run') == True, so the handler runs in dry-run
    mode even though invocation dry_run=False. domain-a.md must not be mutated.
    """
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)
    before_digest = _sha256_file(output_dir / "domain-a.md")

    ex = DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        graph_repair_self_loop="dry_run",
    )
    fixed: List[str] = []
    errors: List[str] = []
    # invocation dry_run=False but per-type=dry_run => effective dry
    ex._run_phase37(output_dir, fixed, errors, dry_run=False)

    after_digest = _sha256_file(output_dir / "domain-a.md")
    assert before_digest == after_digest, (
        "AC8: per-type dry_run must prevent writes even when invocation dry_run=False"
    )
    content_a = (output_dir / "domain-a.md").read_text(encoding="utf-8")
    assert _SELF_LOOP_ROW in content_a, (
        "AC8: self-loop row must still exist (per-type dry_run prevented removal)"
    )


# ---------------------------------------------------------------------------
# Shared helper for bidirectional AC8 test
# ---------------------------------------------------------------------------


def _write_bidirectional_fixture(output_dir: Path) -> None:
    """Write dep-map dir that naturally triggers BIDIRECTIONAL_MISMATCH.

    src.md declares outgoing to tgt; tgt.md has no incoming row.
    The real parser emits AnomalyType.BIDIRECTIONAL_MISMATCH without mocking.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "src.md").write_text(
        "---\nname: src\nparticipating_repos:\n  - repo-src\n---\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| repo-src | api | tgt | code | reason | ev |\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n",
        encoding="utf-8",
    )
    (output_dir / "tgt.md").write_text(
        "---\nname: tgt\nparticipating_repos:\n  - repo-tgt\n---\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n",
        encoding="utf-8",
    )
    (output_dir / "_domains.json").write_text(
        '[{"name":"src","participating_repos":["repo-src"],"last_analyzed":"2024-01-01T00:00:00"},'
        '{"name":"tgt","participating_repos":["repo-tgt"],"last_analyzed":"2024-01-01T00:00:00"}]',
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# AC8 bidirectional precedence test
# ---------------------------------------------------------------------------


def test_bidirectional_invocation_dry_run_suppresses_journal_even_when_per_type_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC8: invocation dry_run=True suppresses journal for BIDIRECTIONAL_MISMATCH even when per_type=enabled.

    Composition rule:
      invocation_dry_run=True, per_type='enabled' => dry_run=True, journal_disabled=True => no journal written.

    Story #919 invocation-level dry_run always wins: even when the per-type flag is 'enabled',
    journaling must be suppressed entirely.
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_bidirectional_fixture(output_dir)

    ex = DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        graph_repair_bidirectional_mismatch="enabled",
        invoke_llm_fn=lambda repo_path, prompt, shell_timeout, outer_timeout: (
            True,
            "VERDICT: INCONCLUSIVE\nEVIDENCE_TYPE: none\nCITATIONS:\nREASONING: stub.\n",
        ),
        repo_path_resolver=lambda alias: "",
    )
    # invocation dry_run=True must suppress journal regardless of per-type flag
    ex._run_phase37(output_dir, [], [], dry_run=True)

    journal_path = tmp_path / "cidx_data" / "dep_map_repair_journal.jsonl"
    assert not journal_path.exists(), (
        "AC8: invocation dry_run=True must suppress journaling for BIDIRECTIONAL_MISMATCH "
        "even when per-type=enabled; journal file must not exist"
    )
