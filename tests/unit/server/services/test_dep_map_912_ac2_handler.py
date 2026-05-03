"""
Story #912 AC2/AC8/AC9: audit_one_bidirectional_mismatch handler tests.

Verifies:
  AC2: exactly one Claude invocation per anomaly (via DI inject)
  AC2: journal entry written per anomaly
  AC8: REFUTED verdict -> journal only, no file write
  AC9: INCONCLUSIVE verdict -> journal only, no file write
  CONFIRMED: backfills incoming row to target .md (real file dirs)
  Claude failure: failure verdict journaled, errors populated

Tests (exhaustive list):
  test_ac2_single_claude_invocation_per_anomaly
  test_ac2_journal_entry_written_per_anomaly
  test_ac8_refuted_and_ac9_inconclusive_no_file_write[refuted]
  test_ac8_refuted_and_ac9_inconclusive_no_file_write[inconclusive]
  test_confirmed_backfills_target_md
  test_claude_failure_journals_entry

Module-level helpers (exhaustive list):
  _FakeAnomaly                                         -- plain class with .message field
  _make_anomaly(src, tgt)                              -- build _FakeAnomaly for src->tgt
  _make_domains(src, tgt)                              -- domains_json with empty repos
  _make_source_md(tmp_path, src, tgt, dep_type)        -- write src.md with outgoing row
  _make_target_md(tmp_path, tgt)                       -- write tgt.md with Incoming section
  _null_resolver(alias)                                -- always returns ""
  _make_refuted_response()                             -- REFUTED Claude response string
  _make_inconclusive_response()                        -- INCONCLUSIVE Claude response string
  _make_confirmed_response(repo_alias, file_path, sym) -- CONFIRMED Claude response string
  _assert_no_file_write_one_journal(target_md, original_content, fixed, journal_path)
  _parse_incoming_source_domains(content)              -- parse Source Domain column values
"""

import json
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.services.dep_map_repair_bidirectional import (
    audit_one_bidirectional_mismatch,
    backfill_target_mirror_row,
)
from code_indexer.server.services.dep_map_repair_phase37 import RepairJournal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeAnomaly:
    """Minimal anomaly object matching the .message interface."""

    def __init__(self, message: str) -> None:
        self.message = message


def _make_anomaly(src: str, tgt: str) -> _FakeAnomaly:
    """Build a BIDIRECTIONAL_MISMATCH anomaly message for src->tgt."""
    return _FakeAnomaly(
        message=(
            f"bidirectional mismatch: {src}→{tgt} declared outgoing by {src}"
            " but not confirmed by incoming table"
        )
    )


def _make_domains(src: str, tgt: str) -> list:
    """Build domains_json with empty participating_repos for src and tgt."""
    return [
        {"name": src, "participating_repos": []},
        {"name": tgt, "participating_repos": []},
    ]


def _make_source_md(
    tmp_path: Path, src: str, tgt: str, dep_type: str = "Code-level"
) -> Path:
    """Write src.md with a minimal outgoing row targeting tgt."""
    md = tmp_path / f"{src}.md"
    md.write_text(
        f"---\nname: {src}\n---\n"
        f"# Domain Analysis: {src}\n\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        f"| repo-a | symbol | {tgt} | {dep_type} | test-why | test-evidence |\n"
        "\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    return md


def _make_target_md(tmp_path: Path, tgt: str) -> Path:
    """Write tgt.md with an empty Incoming Dependencies section."""
    md = tmp_path / f"{tgt}.md"
    md.write_text(
        f"---\nname: {tgt}\n---\n"
        f"# Domain Analysis: {tgt}\n\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    return md


def _null_resolver(alias: str) -> str:
    """Resolver that always returns empty — causes verification gate to downgrade CONFIRMED."""
    return ""


def _make_refuted_response() -> str:
    """Return a minimal REFUTED Claude response string."""
    return (
        "VERDICT: REFUTED\n"
        "EVIDENCE_TYPE: none\n"
        "CITATIONS:\n"
        "REASONING: No evidence found.\n"
    )


def _make_inconclusive_response() -> str:
    """Return a minimal INCONCLUSIVE Claude response string."""
    return (
        "VERDICT: INCONCLUSIVE\n"
        "EVIDENCE_TYPE: none\n"
        "CITATIONS:\n"
        "REASONING: Insufficient evidence.\n"
    )


def _make_confirmed_response(repo_alias: str, file_path: str, sym: str) -> str:
    """Return a CONFIRMED Claude response string with one citation."""
    return (
        "VERDICT: CONFIRMED\n"
        "EVIDENCE_TYPE: code\n"
        "CITATIONS:\n"
        f"  - {repo_alias}:{file_path}:1 {sym}\n"
        f"REASONING: Found {sym} in {file_path}.\n"
    )


def _assert_no_file_write_one_journal(
    target_md: Path,
    original_content: str,
    fixed: List[str],
    journal_path: Path,
) -> None:
    """Assert target .md unchanged, fixed list empty, journal has exactly one entry."""
    assert target_md.read_text() == original_content
    assert fixed == []
    lines = journal_path.read_text().strip().splitlines()
    assert len(lines) == 1


def _parse_incoming_source_domains(content: str) -> List[str]:
    """Extract Source Domain column values from the Incoming Dependencies table.

    Column layout (0-based after pipe split):
      0: External Repo, 1: Depends On, 2: Source Domain, 3: Type, 4: Why, 5: Evidence
    Skips header and separator rows.
    """
    source_domains: List[str] = []
    in_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "### Incoming Dependencies":
            in_section = True
            continue
        if not in_section:
            continue
        if stripped.startswith("#"):
            break
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [c.strip() for c in stripped.split("|")[1:-1]]
        if len(cells) < 3:
            continue
        if cells[0] == "External Repo":
            continue
        if set(cells[0].replace(" ", "")) <= frozenset("-"):
            continue
        source_domains.append(cells[2])
    return source_domains


# ---------------------------------------------------------------------------
# AC2: single invocation + journal entry
# ---------------------------------------------------------------------------


def test_ac2_single_claude_invocation_per_anomaly(tmp_path):
    """AC2: invoke_fn is called exactly once per audit_one_bidirectional_mismatch call."""
    _make_source_md(tmp_path, "src", "tgt")
    _make_target_md(tmp_path, "tgt")
    call_count = [0]

    def counting_invoke(repo_path, prompt, shell_timeout, outer_timeout):
        call_count[0] += 1
        return True, _make_refuted_response()

    journal = RepairJournal(journal_path=tmp_path / "journal.jsonl")
    audit_one_bidirectional_mismatch(
        tmp_path,
        _make_anomaly("src", "tgt"),
        _make_domains("src", "tgt"),
        counting_invoke,
        _null_resolver,
        journal,
        [],
        [],
    )
    assert call_count[0] == 1


def test_ac2_journal_entry_written_per_anomaly(tmp_path):
    """AC2: exactly one journal entry is written per anomaly processed."""
    _make_source_md(tmp_path, "src", "tgt")
    _make_target_md(tmp_path, "tgt")
    journal_path = tmp_path / "journal.jsonl"
    journal = RepairJournal(journal_path=journal_path)

    def invoke_fn(repo_path, prompt, shell_timeout, outer_timeout):
        return True, _make_refuted_response()

    audit_one_bidirectional_mismatch(
        tmp_path,
        _make_anomaly("src", "tgt"),
        _make_domains("src", "tgt"),
        invoke_fn,
        _null_resolver,
        journal,
        [],
        [],
    )
    lines = journal_path.read_text().strip().splitlines()
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# AC8/AC9: REFUTED and INCONCLUSIVE -> journal only (parameterized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response_fn,label",
    [
        (_make_refuted_response, "refuted"),
        (_make_inconclusive_response, "inconclusive"),
    ],
)
def test_ac8_refuted_and_ac9_inconclusive_no_file_write(tmp_path, response_fn, label):
    """AC8/AC9: REFUTED and INCONCLUSIVE verdicts journal one entry but do not write files."""
    _make_source_md(tmp_path, "src", "tgt")
    target_md = _make_target_md(tmp_path, "tgt")
    original_content = target_md.read_text()
    journal_path = tmp_path / f"journal_{label}.jsonl"
    journal = RepairJournal(journal_path=journal_path)
    fixed: List[str] = []

    def invoke_fn(repo_path, prompt, shell_timeout, outer_timeout):
        return True, response_fn()

    audit_one_bidirectional_mismatch(
        tmp_path,
        _make_anomaly("src", "tgt"),
        _make_domains("src", "tgt"),
        invoke_fn,
        _null_resolver,
        journal,
        fixed,
        [],
    )
    _assert_no_file_write_one_journal(target_md, original_content, fixed, journal_path)


# ---------------------------------------------------------------------------
# CONFIRMED: backfill target .md
# ---------------------------------------------------------------------------


def test_confirmed_backfills_target_md(tmp_path):
    """CONFIRMED verdict with passing verification appends incoming row to target .md."""
    sym = "PaymentClient"
    tgt_alias = "tgt-repo"
    src_alias = "src-repo"
    file_path = "src/charge.py"

    tgt_repo = tmp_path / tgt_alias
    (tgt_repo / "src").mkdir(parents=True)
    (tgt_repo / "src" / "charge.py").write_text(f"class {sym}:\n    pass\n")

    src_repo = tmp_path / src_alias
    src_repo.mkdir()
    (src_repo / "caller.py").write_text(f"from tgt import {sym}\n")

    domains = [
        {"name": "src", "participating_repos": [src_alias]},
        {"name": "tgt", "participating_repos": [tgt_alias]},
    ]
    path_map = {tgt_alias: str(tgt_repo), src_alias: str(src_repo)}

    _make_source_md(tmp_path, "src", "tgt")
    target_md = _make_target_md(tmp_path, "tgt")
    journal_path = tmp_path / "journal.jsonl"
    journal = RepairJournal(journal_path=journal_path)
    fixed: List[str] = []

    def invoke_fn(repo_path, prompt, shell_timeout, outer_timeout):
        return True, _make_confirmed_response(tgt_alias, file_path, sym)

    audit_one_bidirectional_mismatch(
        tmp_path,
        _make_anomaly("src", "tgt"),
        domains,
        invoke_fn,
        lambda alias: path_map.get(alias, ""),
        journal,
        fixed,
        [],
    )

    source_domains = _parse_incoming_source_domains(target_md.read_text())
    assert "src" in source_domains
    assert len(fixed) == 1
    assert "tgt.md" in fixed[0]
    lines = journal_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["verdict"] == "CONFIRMED"
    assert len(entry["file_writes"]) == 1


# ---------------------------------------------------------------------------
# Claude failure: journal entry
# ---------------------------------------------------------------------------


def test_claude_failure_journals_entry(tmp_path):
    """invoke_fn returning (False, msg) writes one failure journal entry and appends errors."""
    _make_source_md(tmp_path, "src", "tgt")
    _make_target_md(tmp_path, "tgt")
    journal_path = tmp_path / "journal.jsonl"
    journal = RepairJournal(journal_path=journal_path)
    errors: List[str] = []

    def failing_invoke(repo_path, prompt, shell_timeout, outer_timeout):
        return False, "subprocess timed out"

    audit_one_bidirectional_mismatch(
        tmp_path,
        _make_anomaly("src", "tgt"),
        _make_domains("src", "tgt"),
        failing_invoke,
        _null_resolver,
        journal,
        [],
        errors,
    )
    lines = journal_path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert len(errors) >= 1
    assert "subprocess timed out" in errors[0]


# ---------------------------------------------------------------------------
# H1: idempotent backfill
# ---------------------------------------------------------------------------


def test_backfill_is_idempotent(tmp_path):
    """Calling backfill_target_mirror_row twice with the same arguments writes exactly one row."""
    target_md = _make_target_md(tmp_path, "tgt")
    errors: List[str] = []

    result1 = backfill_target_mirror_row(
        tmp_path,
        "tgt",
        "src",
        ["repo-a"],
        "Code-level",
        "test-why",
        "test-evidence",
        errors,
    )
    result2 = backfill_target_mirror_row(
        tmp_path,
        "tgt",
        "src",
        ["repo-a"],
        "Code-level",
        "test-why",
        "test-evidence",
        errors,
    )

    assert result1 is True
    assert result2 is True  # second call is a no-op, not a failure
    assert errors == []
    source_domains = _parse_incoming_source_domains(target_md.read_text())
    # Exactly one "src" row — idempotent write must not duplicate
    assert source_domains.count("src") == 1
