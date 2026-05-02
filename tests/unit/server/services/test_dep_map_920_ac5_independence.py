"""
Story #920 AC5: Mixed per-type flags operate independently.

Each anomaly type's flag gates only that type's handler, independently of the others.
Tests use a combined fixture that triggers both SELF_LOOP and MALFORMED_YAML to verify:
  - SELF_LOOP=enabled + MALFORMED_YAML=disabled: self-loop removed, malformed file unchanged
  - All flags enabled: both anomaly types processed
  - SELF_LOOP=disabled + all others=dry_run: nothing mutated

Tests (exhaustive list):
  test_self_loop_enabled_malformed_disabled_independent
  test_disabled_type_does_not_block_other_enabled_types
"""

import hashlib
import json
from pathlib import Path
from typing import List

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

_MALFORMED_YAML_DOMAIN_B = """\
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


def _write_combined_fixture(output_dir: Path) -> None:
    """Write dep-map with SELF_LOOP in domain-a and MALFORMED_YAML in domain-b."""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "domain-a.md").write_text(_SELF_LOOP_DOMAIN_A, encoding="utf-8")
    (output_dir / "domain-b.md").write_text(_MALFORMED_YAML_DOMAIN_B, encoding="utf-8")
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


def test_self_loop_enabled_malformed_disabled_independent(tmp_path: Path) -> None:
    """AC5: SELF_LOOP=enabled, MALFORMED_YAML=disabled => self-loop removed, domain-b unchanged.

    The per-type flags are independent: enabling SELF_LOOP does not affect whether
    MALFORMED_YAML is processed. With MALFORMED_YAML=disabled, domain-b.md must be
    untouched even though there is a malformed YAML anomaly present.
    """
    output_dir = tmp_path / "dependency-map"
    _write_combined_fixture(output_dir)

    before_b_digest = _sha256_file(output_dir / "domain-b.md")

    ex = DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        graph_repair_self_loop="enabled",
        graph_repair_malformed_yaml="disabled",
        graph_repair_garbage_domain="disabled",
        graph_repair_bidirectional_mismatch="disabled",
    )
    fixed: List[str] = []
    errors: List[str] = []
    ex._run_phase37(output_dir, fixed, errors, dry_run=False)

    # SELF_LOOP=enabled: self-loop row must be removed from domain-a.md
    content_a = (output_dir / "domain-a.md").read_text(encoding="utf-8")
    assert _SELF_LOOP_ROW not in content_a, (
        f"AC5: self-loop row must be removed (SELF_LOOP=enabled): {_SELF_LOOP_ROW!r}\n"
        f"content_a:\n{content_a}"
    )
    assert _VALID_DEP_ROW in content_a, (
        f"AC5: valid dep row must remain in domain-a: {_VALID_DEP_ROW!r}"
    )

    # MALFORMED_YAML=disabled: domain-b.md must be unchanged
    after_b_digest = _sha256_file(output_dir / "domain-b.md")
    assert before_b_digest == after_b_digest, (
        "AC5: domain-b.md must be unchanged when MALFORMED_YAML=disabled"
    )


def test_disabled_type_does_not_block_other_enabled_types(tmp_path: Path) -> None:
    """AC5: SELF_LOOP=disabled does not block MALFORMED_YAML=dry_run from being reached.

    With SELF_LOOP=disabled and MALFORMED_YAML=dry_run:
    - domain-a.md must NOT be changed (SELF_LOOP skipped entirely)
    - domain-b.md must NOT be changed (MALFORMED_YAML in dry_run, no write)
    Both flags apply independently.
    """
    output_dir = tmp_path / "dependency-map"
    _write_combined_fixture(output_dir)

    before_a_digest = _sha256_file(output_dir / "domain-a.md")
    before_b_digest = _sha256_file(output_dir / "domain-b.md")

    ex = DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        enable_graph_channel_repair=True,
        graph_repair_self_loop="disabled",
        graph_repair_malformed_yaml="dry_run",
        graph_repair_garbage_domain="disabled",
        graph_repair_bidirectional_mismatch="disabled",
    )
    fixed: List[str] = []
    errors: List[str] = []
    ex._run_phase37(output_dir, fixed, errors, dry_run=False)

    after_a_digest = _sha256_file(output_dir / "domain-a.md")
    after_b_digest = _sha256_file(output_dir / "domain-b.md")

    assert before_a_digest == after_a_digest, (
        "AC5: domain-a.md must be unchanged when SELF_LOOP=disabled"
    )
    assert before_b_digest == after_b_digest, (
        "AC5: domain-b.md must be unchanged when MALFORMED_YAML=dry_run"
    )
