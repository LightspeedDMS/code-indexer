"""
Story #920 AC3: Per-type dry_run for all four handler types.

Per-type 'dry_run' composes with is_effective_dry_run:
  - invocation_dry_run=False, per_type='dry_run': no file write, BUT journal IS appended
    with effective_mode='dry_run' (audit trail for operators building confidence).
  - invocation_dry_run=True, per_type='dry_run': no file write, no journal (Story #919
    invocation-level dry_run overrides and suppresses all journaling).

Composition table (relevant rows):
  invocation_dry_run=False, per_type='dry_run' => writes=NO, journal=YES, effective_mode='dry_run'
  invocation_dry_run=True,  per_type='dry_run' => writes=NO, journal=NO (Story #919)

Shared helpers (used by multiple tests below):
  _write_malformed_yaml_fixture    -- dep-map dir with MALFORMED_YAML anomaly
  _write_garbage_domain_fixture    -- dep-map dir with GARBAGE_DOMAIN_REJECTED anomaly
  _make_per_type_executor          -- single parametrized executor factory (avoids duplication)
  _assert_journal_effective_mode   -- shared assertion: anomaly_type has expected effective_mode

Tests (exhaustive list):
  test_per_type_dry_run_self_loop_no_file_write
  test_per_type_dry_run_journals_with_effective_mode_dry_run
  test_malformed_yaml_per_type_dry_run_journals_with_effective_mode_dry_run
  test_garbage_domain_per_type_dry_run_journals_with_effective_mode_dry_run
  test_bidirectional_per_type_dry_run_journals_with_effective_mode_dry_run
  test_journal_entry_default_effective_mode_is_enabled
"""

import hashlib
import json
from pathlib import Path
from typing import Dict
import pytest

from code_indexer.server.services.dep_map_health_detector import DepMapHealthDetector
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator
from code_indexer.server.services.dep_map_repair_executor import DepMapRepairExecutor
from code_indexer.server.services.dep_map_repair_phase37 import Action, JournalEntry


# ---------------------------------------------------------------------------
# Shared fixture writers
# ---------------------------------------------------------------------------


def _write_malformed_yaml_fixture(output_dir: Path) -> None:
    """Write dep-map dir with MALFORMED_YAML anomaly: missing colon on last_analyzed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "domain-a.md").write_text(
        "---\nname: domain-a\nlast_analyzed 2024-01-01T00:00:00\n"
        "participating_repos:\n  - repo-a\n---\n\n## Overview\n\nBody.\n",
        encoding="utf-8",
    )
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
        "# Index\n\n- [domain-a](domain-a.md)\n",
        encoding="utf-8",
    )


def _write_bidirectional_fixture(output_dir: Path) -> None:
    """Write dep-map dir that naturally triggers BIDIRECTIONAL_MISMATCH.

    src.md declares an outgoing dependency to tgt, but tgt.md has no corresponding
    incoming row. The real parser detects this inconsistency and emits
    AnomalyType.BIDIRECTIONAL_MISMATCH without requiring any mocking.
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
        '[{"name":"src","participating_repos":["repo-src"],'
        '"last_analyzed":"2024-01-01T00:00:00"},'
        '{"name":"tgt","participating_repos":["repo-tgt"],'
        '"last_analyzed":"2024-01-01T00:00:00"}]',
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Parametrized executor factory (avoids per-type duplication)
# ---------------------------------------------------------------------------


def _make_per_type_executor(**per_type_flags) -> DepMapRepairExecutor:
    """Build a DepMapRepairExecutor with the given per-type flags.

    Accepted flags: graph_repair_self_loop, graph_repair_malformed_yaml,
    graph_repair_garbage_domain, graph_repair_bidirectional_mismatch,
    invoke_claude_fn, repo_path_resolver.
    """
    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        **per_type_flags,
    )


# ---------------------------------------------------------------------------
# Shared assertion helper
# ---------------------------------------------------------------------------


def _assert_journal_effective_mode(
    journal_path: Path,
    anomaly_type: str,
    expected_mode: str,
) -> None:
    """Assert that the journal contains at least one entry for anomaly_type with expected effective_mode."""
    assert journal_path.exists(), (
        f"AC3: journal must be written for {anomaly_type} per-type=dry_run (invocation dry_run=False)"
    )
    lines = [line for line in journal_path.read_text().splitlines() if line.strip()]
    matching = [
        line for line in lines if json.loads(line).get("anomaly_type") == anomaly_type
    ]
    assert len(matching) >= 1, (
        f"AC3: journal must have at least one {anomaly_type} entry, got all lines: {lines}"
    )
    entry = json.loads(matching[0])
    assert entry.get("effective_mode") == expected_mode, (
        f"AC3: {anomaly_type} journal entry must have effective_mode={expected_mode!r}, "
        f"got: {entry.get('effective_mode')!r}"
    )


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


def _assert_no_file_changes(directory: Path, before: Dict[str, str]) -> None:
    """Assert no files changed between before snapshot and current disk state."""
    after = _sha256_all_files(directory)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert not changed, (
        f"AC3: per-type dry_run must prevent file writes: {sorted(changed)}"
    )


def _make_dry_run_executor() -> DepMapRepairExecutor:
    """Build executor with graph_repair_self_loop='dry_run' and real deps."""
    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        graph_repair_self_loop="dry_run",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_per_type_dry_run_self_loop_no_file_write(tmp_path: Path) -> None:
    """AC3: graph_repair_self_loop='dry_run' with invocation dry_run=False => no file write.

    is_effective_dry_run(False, 'dry_run') == True, so the SELF_LOOP handler is
    invoked in dry-run mode and must not mutate domain-a.md.
    SHA256 of all dep-map files must remain identical.
    """
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)
    before = _sha256_all_files(output_dir)

    ex = _make_dry_run_executor()
    # invocation dry_run=False but per-type self_loop='dry_run' => effective dry
    ex._run_phase37(output_dir, [], [], dry_run=False)

    _assert_no_file_changes(output_dir, before)


def test_per_type_dry_run_journals_with_effective_mode_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: per-type 'dry_run' for SELF_LOOP => journal IS written with effective_mode='dry_run'.

    Story #920 composition rule:
      invocation_dry_run=False, per_type='dry_run' => writes=NO, journal=YES, effective_mode='dry_run'

    Per-type dry_run is a persistent observation mode: writes are skipped but the journal
    IS appended so operators can build confidence before flipping to 'enabled'.
    Only invocation_dry_run=True (Story #919) suppresses journaling entirely.
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)

    ex = _make_dry_run_executor()
    # invocation dry_run=False; per-type self_loop='dry_run' => no file write, but journal IS written
    ex._run_phase37(output_dir, [], [], dry_run=False)

    journal_path = tmp_path / "cidx_data" / "dep_map_repair_journal.jsonl"
    assert journal_path.exists(), (
        "AC3: journal must be written when per-type=dry_run (invocation dry_run=False)"
    )
    lines = [line for line in journal_path.read_text().splitlines() if line.strip()]
    assert len(lines) >= 1, (
        f"AC3: journal must have at least one entry for the SELF_LOOP anomaly, got: {lines}"
    )
    entry = json.loads(lines[0])
    assert entry.get("effective_mode") == "dry_run", (
        f"AC3: journal entry must have effective_mode='dry_run', got: {entry.get('effective_mode')!r}"
    )
    assert entry.get("anomaly_type") == "SELF_LOOP", (
        f"AC3: journal entry must have anomaly_type='SELF_LOOP', got: {entry.get('anomaly_type')!r}"
    )


def test_malformed_yaml_per_type_dry_run_journals_with_effective_mode_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: graph_repair_malformed_yaml='dry_run' + invocation dry_run=False => journal with effective_mode='dry_run'.

    Composition rule: invocation_dry_run=False, per_type='dry_run' => writes=NO, journal=YES, effective_mode='dry_run'.
    No file mutations allowed; MALFORMED_YAML journal entry must have effective_mode='dry_run'.
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_malformed_yaml_fixture(output_dir)
    before = _sha256_all_files(output_dir)

    ex = _make_per_type_executor(graph_repair_malformed_yaml="dry_run")
    ex._run_phase37(output_dir, [], [], dry_run=False)

    _assert_no_file_changes(output_dir, before)
    _assert_journal_effective_mode(
        tmp_path / "cidx_data" / "dep_map_repair_journal.jsonl",
        "MALFORMED_YAML",
        "dry_run",
    )


def test_garbage_domain_per_type_dry_run_journals_with_effective_mode_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: graph_repair_garbage_domain='dry_run' + invocation dry_run=False => journal with effective_mode='dry_run'.

    Composition rule: invocation_dry_run=False, per_type='dry_run' => writes=NO, journal=YES, effective_mode='dry_run'.
    No file mutations allowed; GARBAGE_DOMAIN_REJECTED journal entry must have effective_mode='dry_run'.
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_garbage_domain_fixture(output_dir)
    before = _sha256_all_files(output_dir)

    ex = _make_per_type_executor(graph_repair_garbage_domain="dry_run")
    ex._run_phase37(output_dir, [], [], dry_run=False)

    _assert_no_file_changes(output_dir, before)
    _assert_journal_effective_mode(
        tmp_path / "cidx_data" / "dep_map_repair_journal.jsonl",
        "GARBAGE_DOMAIN_REJECTED",
        "dry_run",
    )


def test_bidirectional_per_type_dry_run_journals_with_effective_mode_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: graph_repair_bidirectional_mismatch='dry_run' + invocation dry_run=False => journal with effective_mode='dry_run'.

    Uses _write_bidirectional_fixture which naturally triggers BIDIRECTIONAL_MISMATCH
    (src declares outgoing to tgt; tgt has no incoming row). Claude stub returns INCONCLUSIVE
    so no backfill write is attempted. Journal must record effective_mode='dry_run'.
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_bidirectional_fixture(output_dir)

    ex = _make_per_type_executor(
        graph_repair_bidirectional_mismatch="dry_run",
        invoke_claude_fn=lambda repo_path, prompt, shell_timeout, outer_timeout: (
            True,
            "VERDICT: INCONCLUSIVE\nEVIDENCE_TYPE: none\nCITATIONS:\nREASONING: stub.\n",
        ),
        repo_path_resolver=lambda alias: "",
    )
    ex._run_phase37(output_dir, [], [], dry_run=False)

    _assert_journal_effective_mode(
        tmp_path / "cidx_data" / "dep_map_repair_journal.jsonl",
        "BIDIRECTIONAL_MISMATCH",
        "dry_run",
    )


def test_journal_entry_default_effective_mode_is_enabled() -> None:
    """JournalEntry must default effective_mode to 'enabled' when not supplied.

    Fix 1: effective_mode: str = 'enabled' default on JournalEntry dataclass.
    Constructing a JournalEntry without effective_mode must produce effective_mode='enabled'.
    """
    entry = JournalEntry(
        anomaly_type="SELF_LOOP",
        source_domain="domain-a",
        target_domain="domain-a",
        source_repos=[],
        target_repos=[],
        verdict="N_A",
        action=Action.self_loop_deleted.value,
        citations=[],
        file_writes=[],
        claude_response_raw="",
    )
    assert entry.effective_mode == "enabled", (
        f"JournalEntry must default effective_mode to 'enabled', got: {entry.effective_mode!r}"
    )
