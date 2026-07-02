"""
Story #908 AC8: Read failure and path-traversal defenses.

Module-level symbols:
- _make_traversal_anomaly(): private helper for path-traversal AnomalyEntry construction
- test_path_traversal_rejected_for_self_loop_anomaly
- test_missing_domain_file_adds_error_without_raising
- test_dispatch_loop_continues_after_error: calls _repair_self_loop for missing then
  real anomaly sequentially — the same pattern the dispatch loop uses — and asserts
  both the error entry (missing) AND the fix entry (real domain) are recorded.
"""

import json
from typing import List

from tests.unit.server.services.test_dep_map_908_builders import (
    make_executor,
    make_self_loop_anomaly,
)
from tests.unit.server.services.test_dep_map_908_helpers import (
    make_domain_with_self_loop,
)


def _make_traversal_anomaly():
    """Create a SELF_LOOP AnomalyEntry with a path-traversal file reference."""
    from code_indexer.server.services.dep_map_parser_hygiene import (
        AnomalyEntry,
        AnomalyType,
    )

    return AnomalyEntry(
        type=AnomalyType.SELF_LOOP,
        file="../../../etc/passwd",
        message="self-loop: evil -> evil",
        channel="data",
        count=1,
    )


def test_path_traversal_rejected_for_self_loop_anomaly(tmp_path):
    """Path-traversal anomaly file is rejected; errors[] receives a Phase 3.7 entry."""
    output_dir = tmp_path / "dependency-map"
    output_dir.mkdir(parents=True, exist_ok=True)
    journal_path = tmp_path / "journal.jsonl"

    executor = make_executor(enable_graph_channel_repair=True)
    errors: List[str] = []
    executor._repair_self_loop(
        output_dir, _make_traversal_anomaly(), [], errors, journal_path=journal_path
    )

    assert len(errors) == 1, f"Expected 1 error entry. Got: {errors}"
    assert "Phase 3.7" in errors[0], f"'Phase 3.7' prefix missing in: {errors[0]!r}"
    assert any(
        word in errors[0].lower() for word in ("unsafe", "traversal", "rejected")
    ), f"Expected traversal-rejection keyword in error: {errors[0]!r}"
    assert not journal_path.exists() or journal_path.read_text().strip() == "", (
        "Journal must not gain entries for a rejected path-traversal anomaly"
    )


def test_missing_domain_file_adds_error_without_raising(tmp_path):
    """Missing .md file produces an error entry; no exception propagates."""
    output_dir = tmp_path / "dependency-map"
    output_dir.mkdir(parents=True, exist_ok=True)
    journal_path = tmp_path / "journal.jsonl"

    executor = make_executor(enable_graph_channel_repair=True)
    errors: List[str] = []
    executor._repair_self_loop(
        output_dir,
        make_self_loop_anomaly("missing-domain"),
        [],
        errors,
        journal_path=journal_path,
    )

    assert len(errors) == 1, f"Expected 1 error entry. Got: {errors}"
    assert "Phase 3.7" in errors[0], f"'Phase 3.7' prefix missing in: {errors[0]!r}"
    assert "missing-domain" in errors[0] or "cannot repair" in errors[0], (
        f"Error must reference the missing file: {errors[0]!r}"
    )


def test_dispatch_loop_continues_after_error(tmp_path):
    """Dispatch loop records error for missing domain then repairs the real domain.

    The dispatch loop calls _repair_self_loop per anomaly.  This test exercises
    the same contract: process missing-domain (records error), then process
    domain-real (records fix). Both sides of the contract are asserted.
    """
    output_dir = tmp_path / "dependency-map"
    output_dir.mkdir(parents=True, exist_ok=True)
    make_domain_with_self_loop(output_dir, "domain-real", ["domain-real"])
    (output_dir / "_domains.json").write_text(
        json.dumps([{"name": "domain-real"}]), encoding="utf-8"
    )
    journal_path = tmp_path / "journal.jsonl"
    executor = make_executor(enable_graph_channel_repair=True)

    fixed: List[str] = []
    errors: List[str] = []

    # Process missing-domain first — records error, does not raise
    executor._repair_self_loop(
        output_dir,
        make_self_loop_anomaly("missing-domain"),
        fixed,
        errors,
        journal_path=journal_path,
    )
    # Process domain-real second — must still succeed despite prior error
    executor._repair_self_loop(
        output_dir,
        make_self_loop_anomaly("domain-real"),
        fixed,
        errors,
        journal_path=journal_path,
    )

    # Error branch: missing-domain must be recorded
    assert len(errors) == 1, f"Expected exactly 1 error (missing-domain). Got: {errors}"
    assert "Phase 3.7" in errors[0], f"Error missing 'Phase 3.7' prefix: {errors[0]!r}"
    assert "missing-domain" in errors[0] or "cannot repair" in errors[0], (
        f"Error does not reference missing-domain: {errors[0]!r}"
    )

    # Continuation: domain-real must still be repaired
    assert len(fixed) == 1, f"Expected exactly 1 fix (domain-real). Got: {fixed}"
    assert "domain-real" in fixed[0], (
        f"Fix does not reference domain-real: {fixed[0]!r}"
    )

    # Self-loop actually gone from domain-real.md
    content = (output_dir / "domain-real.md").read_text(encoding="utf-8")
    targets = [
        cells[3].strip()
        for line in content.splitlines()
        if line.startswith("| repo-a |")
        for cells in [line.split("|")]
        if len(cells) > 3
    ]
    assert "domain-real" not in targets, (
        f"Self-loop still present in domain-real.md. Targets: {targets}"
    )


def test_anomaly_file_used_directly_for_normal_case(tmp_path):
    """AC3 contract: _repair_one_self_loop uses output_dir / anomaly.file as the target.

    Two files exist: file-a.md (has self-loop) and file-b.md (clean, no self-loop).
    anomaly.file points to 'file-a.md'. After repair, only file-a.md is changed.
    file-b.md is untouched, proving the repair targeted anomaly.file directly.
    """
    from code_indexer.server.services.dep_map_parser_hygiene import (
        AnomalyEntry,
        AnomalyType,
    )
    from code_indexer.server.services.dep_map_repair_phase37 import (
        RepairJournal,
        _repair_one_self_loop,
    )

    output_dir = tmp_path / "dependency-map"
    output_dir.mkdir(parents=True, exist_ok=True)

    # file-a.md has a self-loop: target column (cells[3]) == "file-a"
    make_domain_with_self_loop(output_dir, "file-a", ["file-a"])
    file_a = output_dir / "file-a.md"

    # file-b.md is a clean file that must NOT be touched
    file_b = output_dir / "file-b.md"
    file_b_original = "# file-b\n| repo-a | code | other | test dep | evidence |\n"
    file_b.write_text(file_b_original, encoding="utf-8")

    anomaly = AnomalyEntry(
        type=AnomalyType.SELF_LOOP,
        file="file-a.md",
        message="self-loop: file-a -> file-a",
        channel="data",
        count=1,
    )

    fixed: List[str] = []
    errors: List[str] = []
    journal = RepairJournal()
    _repair_one_self_loop(output_dir, anomaly, fixed, errors, journal)

    assert len(errors) == 0, f"Unexpected errors: {errors}"
    assert len(fixed) == 1, f"Expected 1 fix entry, got: {fixed}"
    assert "file-a" in fixed[0], f"Fix entry must reference file-a: {fixed[0]!r}"

    # file-a.md must have the self-loop row removed (cells[3] "file-a" gone)
    updated_a = file_a.read_text(encoding="utf-8")
    self_loop_rows = [
        line
        for line in updated_a.splitlines()
        if line.startswith("|") and line.split("|")[3].strip() == "file-a"
    ]
    assert len(self_loop_rows) == 0, f"Self-loop row still in file-a.md:\n{updated_a}"

    # file-b.md must be completely untouched
    assert file_b.read_text(encoding="utf-8") == file_b_original, (
        "file-b.md was unexpectedly modified by the repair targeting file-a.md"
    )


def test_unexpected_exception_propagates_at_phase37_helpers(tmp_path):
    """MESSI Rule 2 (anti-fallback): RuntimeError must propagate, not be swallowed.

    Patches os.replace inside atomic_write_text to raise RuntimeError.
    After Fix 5 narrows the except to OSError, RuntimeError propagates out.
    Before Fix 5 (broad except Exception), RuntimeError is swallowed — test is RED.
    """
    import pytest
    from unittest.mock import patch

    from code_indexer.server.services.dep_map_repair_phase37 import atomic_write_text

    target = tmp_path / "target.md"
    errors: List[str] = []

    with patch(
        "code_indexer.server.services.dep_map_repair_phase37.os.replace",
        side_effect=RuntimeError("unexpected internal error"),
    ):
        with pytest.raises(RuntimeError, match="unexpected internal error"):
            atomic_write_text(target, "content", errors)
