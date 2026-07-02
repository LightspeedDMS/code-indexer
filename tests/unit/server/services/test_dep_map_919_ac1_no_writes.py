"""
Story #919 AC1: dry_run=True produces zero disk writes.

Verifies:
  AC1: sha256 of every file in output_dir unchanged after _run_phase37(dry_run=True)
  AC4 regression: dry_run=False (default) still mutates files on self-loop fixture
  Uses real filesystem (no pathlib.Path.write_text mock — Rule 1 Anti-Mock)
  Fixtures include SELF_LOOP and MALFORMED_YAML anomaly types

Tests (exhaustive list):
  test_dry_run_no_writes_with_self_loop_anomaly
  test_dry_run_no_writes_with_malformed_yaml_anomaly
  test_dry_run_no_writes_combined_anomalies
  test_normal_run_does_write_self_loop

Module-level helpers (exhaustive list):
  _sha256_all_files(directory)                   -- {rel_path: sha256_hex} for all files
  _make_executor()                               -- DepMapRepairExecutor with real deps
  _assert_dry_run_no_writes(output_dir)          -- run dry_run and assert no changes
  _write_self_loop_fixture(output_dir)           -- dep-map with SELF_LOOP in domain-a
  _write_malformed_yaml_fixture(output_dir)      -- dep-map with MALFORMED_YAML in domain-b
"""

import hashlib
import json
from pathlib import Path
from typing import Dict, List

import pytest

from code_indexer.server.services.dep_map_repair_executor import DepMapRepairExecutor
from code_indexer.server.services.dep_map_health_detector import DepMapHealthDetector
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_all_files(directory: Path) -> Dict[str, str]:
    """Return {relative_path_str: sha256_hex} for all files in directory recursively."""
    result: Dict[str, str] = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            result[str(p.relative_to(directory))] = digest
    return result


def _make_executor() -> DepMapRepairExecutor:
    """Build a DepMapRepairExecutor with real deps and no Claude fn.

    Story #920: graph_repair_self_loop='enabled' so mutation tests exercise the
    real write path. Dry-run tests still pass because invocation dry_run=True
    overrides per-type 'enabled' via is_effective_dry_run.
    """
    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        invoke_llm_fn=None,
        graph_repair_self_loop="enabled",
    )


def _assert_dry_run_no_writes(output_dir: Path) -> None:
    """Run _run_phase37 with dry_run=True and assert no files changed."""
    executor = _make_executor()
    before = _sha256_all_files(output_dir)

    fixed: List[str] = []
    errors: List[str] = []
    executor._run_phase37(output_dir, fixed, errors, dry_run=True)

    after = _sha256_all_files(output_dir)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert not changed, f"Files changed during dry_run: {sorted(changed)}"


_SELF_LOOP_DOMAIN_MD = """\
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

_CLEAN_DOMAIN_B_MD = """\
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

_MALFORMED_YAML_DOMAIN_MD = """\
---
name: domain-b
participating_repos:
  - repo-b
  - [bad yaml
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

_CLEAN_DOMAIN_A_MD = """\
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

### Incoming Dependencies

| External Repo | Depends On | Source Domain | Dep Type | Why | Evidence |
|---|---|---|---|---|---|
"""


def _write_domains_and_index(output_dir: Path) -> None:
    """Write _domains.json and _index.md for a two-domain fixture."""
    domains = [
        {"name": "domain-a", "participating_repos": ["repo-a"]},
        {"name": "domain-b", "participating_repos": ["repo-b"]},
    ]
    (output_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    (output_dir / "_index.md").write_text(
        "# Index\n\n- [domain-a](domain-a.md)\n- [domain-b](domain-b.md)\n",
        encoding="utf-8",
    )


def _write_self_loop_fixture(output_dir: Path) -> None:
    """Write a dep-map directory with a SELF_LOOP anomaly in domain-a."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "domain-a.md").write_text(_SELF_LOOP_DOMAIN_MD, encoding="utf-8")
    (output_dir / "domain-b.md").write_text(_CLEAN_DOMAIN_B_MD, encoding="utf-8")
    _write_domains_and_index(output_dir)


def _write_malformed_yaml_fixture(output_dir: Path) -> None:
    """Write a dep-map directory with a MALFORMED_YAML anomaly in domain-b."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "domain-a.md").write_text(_CLEAN_DOMAIN_A_MD, encoding="utf-8")
    (output_dir / "domain-b.md").write_text(_MALFORMED_YAML_DOMAIN_MD, encoding="utf-8")
    _write_domains_and_index(output_dir)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dry_run_no_writes_with_self_loop_anomaly(tmp_path: Path) -> None:
    """AC1: dry_run=True leaves all files unchanged when SELF_LOOP anomaly present."""
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)
    _assert_dry_run_no_writes(output_dir)


def test_dry_run_no_writes_with_malformed_yaml_anomaly(tmp_path: Path) -> None:
    """AC1: dry_run=True leaves all files unchanged when MALFORMED_YAML anomaly present."""
    output_dir = tmp_path / "dependency-map"
    _write_malformed_yaml_fixture(output_dir)
    _assert_dry_run_no_writes(output_dir)


def test_dry_run_no_writes_combined_anomalies(tmp_path: Path) -> None:
    """AC1: dry_run=True leaves all files unchanged with multiple anomaly types."""
    output_dir = tmp_path / "dependency-map"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "domain-a.md").write_text(_SELF_LOOP_DOMAIN_MD, encoding="utf-8")
    (output_dir / "domain-b.md").write_text(_MALFORMED_YAML_DOMAIN_MD, encoding="utf-8")
    _write_domains_and_index(output_dir)
    _assert_dry_run_no_writes(output_dir)


def test_normal_run_does_write_self_loop(tmp_path: Path) -> None:
    """AC4 regression: dry_run=False (default) mutates files — self-loop is removed."""
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)

    executor = _make_executor()
    before_domain_a = hashlib.sha256(
        (output_dir / "domain-a.md").read_bytes()
    ).hexdigest()

    fixed: List[str] = []
    errors: List[str] = []
    executor._run_phase37(output_dir, fixed, errors, dry_run=False)

    after_domain_a = hashlib.sha256(
        (output_dir / "domain-a.md").read_bytes()
    ).hexdigest()
    assert before_domain_a != after_domain_a, (
        "Expected domain-a.md to change after normal run (self-loop removal)"
    )


def test_dry_run_does_not_write_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: dry_run=True must NOT write to the repair journal.

    Isolates CIDX_DATA_DIR to a tmp subdirectory so any journal write is
    detectable. With the temp-copy approach the journal writes to the global
    path because RepairJournal resolves its path outside the temp copy.
    The per-handler gating approach fixes this by never calling
    journal.append() when dry_run=True.
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))
    output_dir = tmp_path / "dependency-map"
    _write_self_loop_fixture(output_dir)

    executor = _make_executor()
    fixed: List[str] = []
    errors: List[str] = []
    executor._run_phase37(output_dir, fixed, errors, dry_run=True)

    journal_path = tmp_path / "cidx_data" / "dep_map_repair_journal.jsonl"
    assert not journal_path.exists(), (
        f"Journal must not be written during dry_run but found: {journal_path}"
    )


# domain-a has an outgoing row whose Target Domain value "depends on repo-b (internal)"
# is a prose fragment (contains parentheses) that is_prose_fragment() returns True for.
# The parser emits a GARBAGE_DOMAIN_REJECTED anomaly with a message containing "repo-b",
# which maps to "domain-b" in the inverted index — so the repair code reaches the
# journal_and_backfill_fn call, which previously crashed with TypeError when journal=None.
_GARBAGE_DOMAIN_A_MD = """\
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
| repo-a | code | depends on repo-b (internal) | why | evidence |

### Incoming Dependencies

| External Repo | Depends On | Source Domain | Dep Type | Why | Evidence |
|---|---|---|---|---|---|
"""


def _write_garbage_domain_fixture(output_dir: Path) -> None:
    """Write a dep-map directory with a GARBAGE_DOMAIN_REJECTED anomaly in domain-a."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "domain-a.md").write_text(_GARBAGE_DOMAIN_A_MD, encoding="utf-8")
    (output_dir / "domain-b.md").write_text(_CLEAN_DOMAIN_B_MD, encoding="utf-8")
    _write_domains_and_index(output_dir)


def test_dry_run_no_writes_with_garbage_domain_anomaly(tmp_path: Path) -> None:
    """AC1 Blocker 2 regression: dry_run=True must not crash or write files with GARBAGE_DOMAIN_REJECTED.

    Regression guard for the TypeError raised when journal_and_backfill_garbage_domain
    was called with journal=None during dry-run (validate guard ran before dry_run check).
    """
    output_dir = tmp_path / "dependency-map"
    _write_garbage_domain_fixture(output_dir)
    _assert_dry_run_no_writes(output_dir)


# BIDIRECTIONAL_MISMATCH fixture: domain-a has outgoing to domain-b but domain-b
# has no incoming row confirming it — triggers BIDIRECTIONAL_MISMATCH in the parser.
_BIDI_DOMAIN_A_MD = """\
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
| repo-a | code | domain-b | depends on it | evidence |

### Incoming Dependencies

| External Repo | Depends On | Source Domain | Dep Type | Why | Evidence |
|---|---|---|---|---|---|
"""

_BIDI_DOMAIN_B_NO_INCOMING_MD = """\
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


def _make_confirmed_claude_response(repo: str, file_path: str, symbol: str) -> str:
    """Build a CONFIRMED Claude response citing the given repo/file/symbol."""
    return (
        "VERDICT: CONFIRMED\n"
        "EVIDENCE_TYPE: code\n"
        "CITATIONS:\n"
        f"- {repo}:{file_path}:1 {symbol}\n"
        "REASONING: dependency confirmed by real file inspection\n"
    )


def _make_executor_with_real_bidi_stubs(
    src_repo: Path, repo_alias: str, file_path: str, symbol: str
) -> DepMapRepairExecutor:
    """Executor with CONFIRMED claude fn citing a real file, so rg verification passes.

    src_repo must already exist and contain <file_path> with <symbol>.
    repo_path_resolver returns src_repo for repo_alias, nonexistent for others.
    """
    confirmed_response = _make_confirmed_claude_response(repo_alias, file_path, symbol)

    def stub_invoke_claude(
        repo_path: str, prompt: str, shell_timeout: int, outer_timeout: int
    ) -> tuple:
        return True, confirmed_response

    def stub_repo_path_resolver(alias: str) -> str:
        if alias == repo_alias:
            return str(src_repo)
        return "/nonexistent/" + alias

    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        invoke_llm_fn=stub_invoke_claude,
        repo_path_resolver=stub_repo_path_resolver,
    )


def _run_bidi_dry_run_report(output_dir: Path, executor: DepMapRepairExecutor):
    """Run _run_phase37 dry_run=True and return (report, fixed, errors)."""
    fixed: List[str] = []
    errors: List[str] = []
    report = executor._run_phase37(output_dir, fixed, errors, dry_run=True)
    return report, fixed, errors


def _assert_bidi_dry_run_no_writes(before: Dict[str, str], output_dir: Path) -> None:
    """Assert no files changed between before snapshot and current disk state."""
    after = _sha256_all_files(output_dir)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert not changed, f"Files changed during dry_run: {sorted(changed)}"


def _write_bidi_mismatch_fixture(output_dir: Path) -> None:
    """Write dep-map with a BIDIRECTIONAL_MISMATCH anomaly (domain-a->domain-b unconfirmed)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "domain-a.md").write_text(_BIDI_DOMAIN_A_MD, encoding="utf-8")
    (output_dir / "domain-b.md").write_text(
        _BIDI_DOMAIN_B_NO_INCOMING_MD, encoding="utf-8"
    )
    domains = [
        {"name": "domain-a", "participating_repos": ["repo-a"]},
        {"name": "domain-b", "participating_repos": ["repo-b"]},
    ]
    (output_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    (output_dir / "_index.md").write_text("# Index\n", encoding="utf-8")


def test_bidi_confirmed_dry_run_no_writes_but_reports_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 1+2: dry_run=True with CONFIRMED BIDIRECTIONAL_MISMATCH verdict.

    Uses a real temp directory acting as the source repo, with a real file containing
    the cited symbol, so rg actually finds it (AC6+AC7+AC11 all pass).
    The CONFIRMED verdict must survive the verification gate.

    Assertions (Blocker 1 double-count guard):
      - per_type_counts["bidirectional_mismatch"] == 1
      - per_verdict_counts["CONFIRMED"] == 1  (NOT 2 — verifies Blocker 1 fix)
      - per_action_counts["auto_backfilled"] == 1  (NOT 2)
      - would_be_writes contains one bidirectional_mirror_backfilled operation
      - sha256 of all output_dir files unchanged (no disk write)
      - journal does NOT exist under isolated CIDX_DATA_DIR
    """
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx_data"))

    # Build a real source repo with the cited symbol so rg can verify it.
    src_repo = tmp_path / "src_repo"
    src_repo_src_dir = src_repo / "src"
    src_repo_src_dir.mkdir(parents=True)
    symbol = "order_api.process"
    cited_file = "src/consumer.py"
    (src_repo_src_dir / "consumer.py").write_text(
        f"def consumer():\n    {symbol}()\n", encoding="utf-8"
    )

    output_dir = tmp_path / "dependency-map"
    _write_bidi_mismatch_fixture(output_dir)
    before = _sha256_all_files(output_dir)

    executor = _make_executor_with_real_bidi_stubs(
        src_repo, "repo-a", cited_file, symbol
    )
    report, _fixed, _errors = _run_bidi_dry_run_report(output_dir, executor)

    # No disk writes — dry_run=True must not mutate any output_dir files.
    _assert_bidi_dry_run_no_writes(before, output_dir)

    # No journal written — journal is skipped during dry_run.
    journal_path = tmp_path / "cidx_data" / "dep_map_repair_journal.jsonl"
    assert not journal_path.exists(), (
        f"Journal must not exist during dry_run but found: {journal_path}"
    )

    assert report is not None, "dry_run=True must return a DryRunReport"

    # Anomaly surfaced in per_type_counts.
    assert report.per_type_counts.get("bidirectional_mismatch", 0) == 1, (
        f"per_type_counts missing bidirectional_mismatch: {report.per_type_counts}"
    )

    # Blocker 1: CONFIRMED counted exactly once (not double-counted from fixed[] + extras).
    assert report.per_verdict_counts.get("CONFIRMED", 0) == 1, (
        f"Expected exactly 1 CONFIRMED verdict (double-count regression): {report.per_verdict_counts}"
    )
    assert report.per_action_counts.get("auto_backfilled", 0) == 1, (
        f"Expected exactly 1 auto_backfilled action (double-count regression): {report.per_action_counts}"
    )

    # would_be_writes contains the bidirectional backfill operation (dry_run only logs it).
    assert len(report.would_be_writes) == 1, (
        f"Expected 1 would_be_write for CONFIRMED backfill: {report.would_be_writes}"
    )
    _op_label = report.would_be_writes[0][1]
    assert _op_label == "bidirectional_mirror_backfilled", (
        f"would_be_writes operation label wrong: {_op_label!r}"
    )


def test_bidi_dry_run_invokes_claude_fn(tmp_path: Path) -> None:
    """Blocker 1: _audit_bidirectional_mismatch must invoke claude_fn during dry_run=True.

    Regression guard: previously _audit_bidirectional_mismatch returned immediately
    when dry_run=True without calling Claude. The dry-run should run Claude+ripgrep
    but skip the backfill write.
    """
    output_dir = tmp_path / "dependency-map"
    _write_bidi_mismatch_fixture(output_dir)

    claude_calls: List[str] = []

    def stub_invoke_claude(repo_path: str, prompt: str, *args, **kwargs):
        claude_calls.append(repo_path)
        # Return REFUTED so no backfill write is attempted
        return False, "REFUTED: no evidence found"

    executor = DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        invoke_llm_fn=stub_invoke_claude,
    )
    fixed: List[str] = []
    errors: List[str] = []
    executor._run_phase37(output_dir, fixed, errors, dry_run=True)

    assert len(claude_calls) > 0, (
        "Blocker 1: stub_invoke_claude was never called during dry_run=True — "
        "BIDIRECTIONAL_MISMATCH audit was silently skipped"
    )
