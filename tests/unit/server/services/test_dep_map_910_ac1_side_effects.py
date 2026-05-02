"""
Story #910 AC1: Side-effect assertions for _repair_malformed_yaml.

Verifies that fixed[], _log (via journal_callback seam), body bytes,
and JSONL journal file are all correctly handled during a successful
surgical frontmatter re-emit.

TestAC1SideEffects (4 methods):
  test_fixed_list_equals_exactly_one_reemit_entry
  test_log_flows_through_journal_callback_with_reemit_wording
  test_body_bytes_byte_identical_after_repair
  test_journal_file_contains_malformed_yaml_reemitted_entry
"""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, List, Tuple

import pytest

from tests.unit.server.services.test_dep_map_910_builders import (
    make_domains_json,
    make_executor_910,
    make_malformed_domain_file,
    make_malformed_yaml_anomaly,
)
from tests.unit.server.services.test_dep_map_910_helpers import extract_body_bytes

if TYPE_CHECKING:
    from tests.unit.server.services.test_dep_map_910_builders import (
        AnomalyEntry,
        DepMapRepairExecutor,
    )


@pytest.fixture()
def ac1_fixture(tmp_path) -> Tuple[Path, "DepMapRepairExecutor", "AnomalyEntry"]:
    """Return (output_dir, executor, anomaly) for AC1 side-effect tests."""
    output_dir = tmp_path / "dependency-map"
    output_dir.mkdir(parents=True, exist_ok=True)

    domain_info = {
        "name": "domain-z",
        "last_analyzed": "2024-06-01T12:00:00",
        "participating_repos": ["repo-x", "repo-y"],
    }
    make_malformed_domain_file(output_dir, "domain-z")
    make_domains_json(output_dir, [domain_info])

    executor = make_executor_910()
    anomaly = make_malformed_yaml_anomaly("domain-z.md")
    return output_dir, executor, anomaly


class TestAC1SideEffects:
    """AC1 side-effects: exact fixed[], _log via journal_callback, byte-identical body,
    and JSONL journal entry written with action=malformed_yaml_reemitted."""

    def test_fixed_list_equals_exactly_one_reemit_entry(self, ac1_fixture):
        """fixed[] is exactly ['Phase 3.7: re-emitted frontmatter for domain-z']."""
        output_dir, executor, anomaly = ac1_fixture
        fixed: List[str] = []
        errors: List[str] = []

        executor._repair_malformed_yaml(output_dir, anomaly, fixed, errors)

        assert fixed == ["Phase 3.7: re-emitted frontmatter for domain-z"], (
            f"fixed list mismatch: {fixed}"
        )

    def test_log_flows_through_journal_callback_with_reemit_wording(self, ac1_fixture):
        """_log re-emit message flows through injected journal_callback external seam.

        Observes through journal_callback (injected at construction) not via
        SUT patching — satisfies MESSI rule 1 anti-mock posture.
        """
        output_dir, _, anomaly = ac1_fixture
        log_messages: List[str] = []
        executor_with_callback = make_executor_910(journal_callback=log_messages.append)

        executor_with_callback._repair_malformed_yaml(output_dir, anomaly, [], [])

        matching = [
            m
            for m in log_messages
            if ("re-emit" in m.lower() or "re-emitted" in m.lower()) and "domain-z" in m
        ]
        assert matching, (
            f"Expected journal_callback message with 're-emit'/'re-emitted' and 'domain-z'. "
            f"All messages: {log_messages}"
        )

    def test_body_bytes_byte_identical_after_repair(self, ac1_fixture):
        """Body bytes after closing --- are byte-identical via read_bytes() slice."""
        output_dir, executor, anomaly = ac1_fixture
        md_path = output_dir / "domain-z.md"
        original_body_bytes = extract_body_bytes(md_path.read_bytes())

        executor._repair_malformed_yaml(output_dir, anomaly, [], [])

        repaired_body_bytes = extract_body_bytes(md_path.read_bytes())
        assert repaired_body_bytes == original_body_bytes, (
            f"Body bytes changed.\nOriginal: {original_body_bytes!r}\n"
            f"Repaired: {repaired_body_bytes!r}"
        )

    def test_journal_file_contains_malformed_yaml_reemitted_entry(self, tmp_path):
        """JSONL journal receives entry with action='malformed_yaml_reemitted' on success.

        Sets CIDX_DATA_DIR to tmp_path so the journal writes to a controlled
        location isolated from other test runs. Verifies the real journal file
        on disk — not a callback seam — proving the executor wires journal writes.
        """
        journal_dir = tmp_path / "cidx-data"
        journal_dir.mkdir(parents=True, exist_ok=True)
        output_dir = tmp_path / "dependency-map"
        output_dir.mkdir(parents=True, exist_ok=True)

        domain_info = {
            "name": "domain-z",
            "last_analyzed": "2024-06-01T12:00:00",
            "participating_repos": ["repo-x", "repo-y"],
        }
        make_malformed_domain_file(output_dir, "domain-z")
        make_domains_json(output_dir, [domain_info])

        executor = make_executor_910()
        anomaly = make_malformed_yaml_anomaly("domain-z.md")

        old_cidx_data_dir = os.environ.get("CIDX_DATA_DIR")
        try:
            os.environ["CIDX_DATA_DIR"] = str(journal_dir)
            executor._repair_malformed_yaml(output_dir, anomaly, [], [])
        finally:
            if old_cidx_data_dir is None:
                os.environ.pop("CIDX_DATA_DIR", None)
            else:
                os.environ["CIDX_DATA_DIR"] = old_cidx_data_dir

        journal_path = journal_dir / "dep_map_repair_journal.jsonl"
        assert journal_path.exists(), f"Journal file not created at {journal_path}"
        entries = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        actions = [e.get("action") for e in entries]
        assert "malformed_yaml_reemitted" in actions, (
            f"Expected 'malformed_yaml_reemitted' in journal actions, got: {actions}"
        )
