"""
Story #920 AC4: Per-type enabled for SELF_LOOP => file mutation occurs (self-loop row removed).

When graph_repair_self_loop='enabled' and invocation dry_run=False,
is_effective_dry_run(False, 'enabled') == False, so the SELF_LOOP handler
must actually remove the self-loop row from the domain file while leaving
the valid dependency row intact. The journal entry must record effective_mode='enabled'.

Tests (exhaustive list):
  test_per_type_enabled_self_loop_row_removed_valid_row_preserved
  test_per_type_enabled_self_loop_fixed_entry_added
  test_per_type_enabled_journal_entry_has_effective_mode_enabled
"""

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


def _make_enabled_executor() -> DepMapRepairExecutor:
    """Build executor with graph_repair_self_loop='enabled' and real deps."""
    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        graph_repair_self_loop="enabled",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_per_type_enabled_self_loop_row_removed_valid_row_preserved(
    tmp_path: Path,
) -> None:
    """AC4: graph_repair_self_loop='enabled' + invocation dry_run=False => self-loop row removed.

    is_effective_dry_run(False, 'enabled') == False so the handler runs in full mode.
    Asserts:
      - self-loop row no longer present in domain-a.md
      - valid dependency row (domain-a -> domain-b) still present in domain-a.md
    """
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)

    ex = _make_enabled_executor()
    fixed: List[str] = []
    errors: List[str] = []
    ex._run_phase37(output_dir, fixed, errors, dry_run=False)

    content = (output_dir / "domain-a.md").read_text(encoding="utf-8")
    assert _SELF_LOOP_ROW not in content, (
        f"AC4: self-loop row must be removed from domain-a.md after enabled repair.\n"
        f"Row still present: {_SELF_LOOP_ROW!r}\nFile content:\n{content}"
    )
    assert _VALID_DEP_ROW in content, (
        f"AC4: valid dependency row must remain in domain-a.md after self-loop repair.\n"
        f"Row missing: {_VALID_DEP_ROW!r}\nFile content:\n{content}"
    )


def test_per_type_enabled_self_loop_fixed_entry_added(tmp_path: Path) -> None:
    """AC4: graph_repair_self_loop='enabled' + invocation dry_run=False => fixed[] entry added."""
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)

    ex = _make_enabled_executor()
    fixed: List[str] = []
    errors: List[str] = []
    ex._run_phase37(output_dir, fixed, errors, dry_run=False)

    assert len(fixed) >= 1, (
        f"AC4: fixed[] must have at least one entry when self-loop is repaired, got: {fixed}"
    )
    self_loop_fixes = [f for f in fixed if "self" in f.lower() or "loop" in f.lower()]
    assert len(self_loop_fixes) >= 1, (
        f"AC4: no self-loop fix description in fixed[], got: {fixed}"
    )


def test_per_type_enabled_journal_entry_has_effective_mode_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: graph_repair_self_loop='enabled' + invocation dry_run=False => journal entry has effective_mode='enabled'.

    Story #920 composition rule:
      invocation_dry_run=False, per_type='enabled' => writes=YES, journal=YES, effective_mode='enabled'

    Asserts:
      - journal file exists after a successful SELF_LOOP repair
      - the journal entry records effective_mode='enabled'
      - the journal entry records anomaly_type='SELF_LOOP'
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)

    ex = _make_enabled_executor()
    fixed: List[str] = []
    errors: List[str] = []
    ex._run_phase37(output_dir, fixed, errors, dry_run=False)

    journal_path = tmp_path / "cidx_data" / "dep_map_repair_journal.jsonl"
    assert journal_path.exists(), (
        "AC4: journal must be written when per-type=enabled (invocation dry_run=False)"
    )
    lines = [line for line in journal_path.read_text().splitlines() if line.strip()]
    assert len(lines) >= 1, (
        f"AC4: journal must have at least one entry for the SELF_LOOP repair, got: {lines}"
    )
    entry = json.loads(lines[0])
    assert entry.get("effective_mode") == "enabled", (
        f"AC4: journal entry must have effective_mode='enabled', got: {entry.get('effective_mode')!r}"
    )
    assert entry.get("anomaly_type") == "SELF_LOOP", (
        f"AC4: journal entry must have anomaly_type='SELF_LOOP', got: {entry.get('anomaly_type')!r}"
    )


# ---------------------------------------------------------------------------
# Shared helpers for MALFORMED_YAML / GARBAGE / BIDIRECTIONAL AC4 tests
# ---------------------------------------------------------------------------


def _write_malformed_yaml_fixture(output_dir: Path) -> None:
    """Write dep-map dir with MALFORMED_YAML anomaly: missing colon on last_analyzed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "domain-a.md").write_text(
        "---\nname: domain-a\nlast_analyzed 2024-01-01T00:00:00\n"
        "participating_repos:\n  - repo-a\n---\n\n## Overview\n\nBody.\n",
        encoding="utf-8",
    )
    domains = [{"name": "domain-a", "participating_repos": ["repo-a"]}]
    (output_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    (output_dir / "_index.md").write_text(
        "# Index\n\n- [domain-a](domain-a.md)\n", encoding="utf-8"
    )


def _write_garbage_domain_fixture(output_dir: Path) -> None:
    """Write dep-map dir with GARBAGE_DOMAIN_REJECTED anomaly.

    Uses "repo-a (broken)" as target domain value. Parentheses trigger
    is_prose_fragment=True. The anomaly message contains token "repo-a"
    which maps uniquely to domain-a via the inverted repo index, so the
    repair can resolve a single candidate target domain.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "domain-a.md").write_text(
        "---\nname: domain-a\nparticipating_repos:\n  - repo-a\n---\n\n"
        "## Dependencies\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| repo-a | api | repo-a (broken) | Service | legacy | ref-1 |\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n",
        encoding="utf-8",
    )
    domains = [{"name": "domain-a", "participating_repos": ["repo-a"]}]
    (output_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    (output_dir / "_index.md").write_text(
        "# Index\n\n- [domain-a](domain-a.md)\n", encoding="utf-8"
    )


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


def _make_per_type_executor(**per_type_flags) -> DepMapRepairExecutor:
    """Build a DepMapRepairExecutor with the given per-type flags."""
    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        **per_type_flags,
    )


def _assert_journal_effective_mode_ac4(
    journal_path: Path, anomaly_type: str, expected_mode: str
) -> None:
    """Assert journal has at least one entry for anomaly_type with expected effective_mode."""
    assert journal_path.exists(), (
        f"AC4: journal must be written for {anomaly_type} per-type=enabled (invocation dry_run=False)"
    )
    lines = [line for line in journal_path.read_text().splitlines() if line.strip()]
    matching = [
        line for line in lines if json.loads(line).get("anomaly_type") == anomaly_type
    ]
    assert len(matching) >= 1, (
        f"AC4: journal must have at least one {anomaly_type} entry, all lines: {lines}"
    )
    got = json.loads(matching[0]).get("effective_mode")
    assert got == expected_mode, (
        f"AC4: {anomaly_type} journal entry must have effective_mode={expected_mode!r}, got: {got!r}"
    )


# ---------------------------------------------------------------------------
# AC4 tests for MALFORMED_YAML, GARBAGE_DOMAIN_REJECTED, BIDIRECTIONAL_MISMATCH
# ---------------------------------------------------------------------------


def test_malformed_yaml_per_type_enabled_journal_entry_has_effective_mode_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: graph_repair_malformed_yaml='enabled' + invocation dry_run=False => journal with effective_mode='enabled'.

    Composition rule: invocation_dry_run=False, per_type='enabled' => writes=YES, journal=YES, effective_mode='enabled'.
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_malformed_yaml_fixture(output_dir)

    ex = _make_per_type_executor(graph_repair_malformed_yaml="enabled")
    ex._run_phase37(output_dir, [], [], dry_run=False)

    _assert_journal_effective_mode_ac4(
        tmp_path / "cidx_data" / "dep_map_repair_journal.jsonl",
        "MALFORMED_YAML",
        "enabled",
    )


def test_garbage_domain_per_type_enabled_journal_entry_has_effective_mode_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: graph_repair_garbage_domain='enabled' + invocation dry_run=False => journal with effective_mode='enabled'.

    Composition rule: invocation_dry_run=False, per_type='enabled' => writes=YES, journal=YES, effective_mode='enabled'.
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_garbage_domain_fixture(output_dir)

    ex = _make_per_type_executor(graph_repair_garbage_domain="enabled")
    ex._run_phase37(output_dir, [], [], dry_run=False)

    _assert_journal_effective_mode_ac4(
        tmp_path / "cidx_data" / "dep_map_repair_journal.jsonl",
        "GARBAGE_DOMAIN_REJECTED",
        "enabled",
    )


def test_bidirectional_per_type_enabled_journal_entry_has_effective_mode_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: graph_repair_bidirectional_mismatch='enabled' + invocation dry_run=False => journal with effective_mode='enabled'.

    Uses _write_bidirectional_fixture which naturally triggers BIDIRECTIONAL_MISMATCH.
    Claude stub returns INCONCLUSIVE so no backfill write is attempted.
    Journal must record effective_mode='enabled'.
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_bidirectional_fixture(output_dir)

    ex = _make_per_type_executor(
        graph_repair_bidirectional_mismatch="enabled",
        invoke_llm_fn=lambda repo_path, prompt, shell_timeout, outer_timeout: (
            True,
            "VERDICT: INCONCLUSIVE\nEVIDENCE_TYPE: none\nCITATIONS:\nREASONING: stub.\n",
        ),
        repo_path_resolver=lambda alias: "",
    )
    ex._run_phase37(output_dir, [], [], dry_run=False)

    _assert_journal_effective_mode_ac4(
        tmp_path / "cidx_data" / "dep_map_repair_journal.jsonl",
        "BIDIRECTIONAL_MISMATCH",
        "enabled",
    )
