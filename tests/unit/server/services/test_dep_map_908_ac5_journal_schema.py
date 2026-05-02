"""
Story #908 AC5: Journal entry schema and action enum coverage for SELF_LOOP.

Module-level test functions (no class wrapper) to respect the 3-method-per-class
operation limit. Tests (6 total):
- test_journal_entry_has_all_12_fields
- test_invalid_action_raises_value_error
- test_invalid_verdict_raises_value_error
- test_journal_line_is_single_line_valid_json
- test_timestamp_is_iso8601_with_timezone
- test_action_enum_has_self_loop_deleted
"""

import json
from datetime import datetime

import pytest

from tests.unit.server.services.test_dep_map_908_entry_builder import make_journal_entry

_EXPECTED_SCHEMA_KEYS = frozenset(
    {
        "timestamp",
        "anomaly_type",
        "source_domain",
        "target_domain",
        "source_repos",
        "target_repos",
        "verdict",
        "action",
        "citations",
        "file_writes",
        "claude_response_raw",
        "effective_mode",
    }
)


def test_journal_entry_has_all_12_fields():
    """JournalEntry serializes to a dict with exactly the 12 documented schema fields."""
    entry = make_journal_entry()
    serialized = json.loads(entry.serialize())
    assert set(serialized.keys()) == _EXPECTED_SCHEMA_KEYS, (
        f"Schema mismatch.\n"
        f"Expected: {sorted(_EXPECTED_SCHEMA_KEYS)}\n"
        f"Got:      {sorted(serialized.keys())}"
    )


def test_invalid_action_raises_value_error():
    """Invalid action raises ValueError at JournalEntry construction."""
    from code_indexer.server.services.dep_map_repair_executor import JournalEntry

    with pytest.raises(ValueError, match="action"):
        JournalEntry(
            anomaly_type="SELF_LOOP",
            source_domain="a",
            target_domain="a",
            source_repos=[],
            target_repos=[],
            verdict="N_A",
            action="not_a_real_action",
            citations=[],
            file_writes=[],
            claude_response_raw="",
            effective_mode="enabled",
        )


def test_invalid_verdict_raises_value_error():
    """Invalid verdict raises ValueError at JournalEntry construction."""
    from code_indexer.server.services.dep_map_repair_executor import JournalEntry

    with pytest.raises(ValueError, match="verdict"):
        JournalEntry(
            anomaly_type="SELF_LOOP",
            source_domain="a",
            target_domain="a",
            source_repos=[],
            target_repos=[],
            verdict="NOT_VALID",
            action="self_loop_deleted",
            citations=[],
            file_writes=[],
            claude_response_raw="",
            effective_mode="enabled",
        )


def test_journal_line_is_single_line_valid_json():
    """serialize() returns valid JSON on a single line ending with newline."""
    entry = make_journal_entry()
    line = entry.serialize()

    assert line.endswith("\n"), "Serialized entry must end with \\n"
    assert "\n" not in line[:-1], "Serialized entry must have no embedded newlines"
    assert isinstance(json.loads(line), dict), "Serialized line must parse as a dict"


def test_timestamp_is_iso8601_with_timezone():
    """timestamp field is ISO 8601 with timezone information."""
    entry = make_journal_entry()
    serialized = json.loads(entry.serialize())
    dt = datetime.fromisoformat(serialized["timestamp"])
    assert dt.tzinfo is not None, (
        f"timestamp has no timezone: {serialized['timestamp']!r}"
    )


def test_action_enum_has_self_loop_deleted():
    """Action enum exposes 'self_loop_deleted' member with correct string value."""
    from code_indexer.server.services.dep_map_repair_executor import Action

    assert hasattr(Action, "self_loop_deleted"), (
        "Action enum missing 'self_loop_deleted' member"
    )
    assert Action.self_loop_deleted.value == "self_loop_deleted"


def test_file_writes_inner_schema_has_path_and_operation(tmp_path):
    """file_writes entries must use {path: str, operation: str} — not {file, action}.

    Codex remediation item 2: spec says list[{path: str, operation: str}].
    Exercises build_and_append_journal_entry which populates file_writes.
    """
    import json

    from code_indexer.server.services.dep_map_repair_phase37 import (
        RepairJournal,
        build_and_append_journal_entry,
    )

    md_path = tmp_path / "domain-x.md"
    md_path.write_text("# domain-x\n", encoding="utf-8")
    journal_path = tmp_path / "journal.jsonl"
    journal = RepairJournal(journal_path=journal_path)

    build_and_append_journal_entry(md_path, "domain-x", None, journal, errors=[])

    lines = journal_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, f"Expected 1 journal line, got {len(lines)}"

    entry = json.loads(lines[0])
    file_writes = entry.get("file_writes", [])
    assert len(file_writes) == 1, f"Expected 1 file_write entry, got {file_writes}"

    fw = file_writes[0]
    assert set(fw.keys()) == {"path", "operation"}, (
        f"file_writes entry must have exactly 'path' and 'operation' keys. "
        f"Got: {set(fw.keys())}"
    )
    assert isinstance(fw["path"], str) and fw["path"], (
        f"'path' must be a non-empty str, got: {fw['path']!r}"
    )
    assert isinstance(fw["operation"], str) and fw["operation"], (
        f"'operation' must be a non-empty str, got: {fw['operation']!r}"
    )
