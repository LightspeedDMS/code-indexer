"""
Regression tests for Bug #687: _domains.json description/evidence drift.

Zero mocks for core logic. domain_analyzer (Claude CLI) is a test double
because it is external and expensive.

Scenarios:
  A: 6 domains with empty JSON metadata but valid .md files (production incident)
  B: Incremental new-domain creation sets needs_reanalysis or non-empty description
  C: Phase 3.5 backfill populates JSON from .md frontmatter / ## Overview
  D: Orphan .md files get JSON entries with backfilled description after repair
  E: Repair is idempotent — second run leaves state unchanged
  F: Partial drift — only broken domains updated, healthy ones unchanged
  G: Pass 2 frontmatter includes description; full pipeline has no empty cells
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest

from tests.unit.server.services.test_dep_map_health_detector import make_index_md

# ─────────────────────────────────────────────────────────────────────────────
# Real service factories
# ─────────────────────────────────────────────────────────────────────────────


def _health_detector():
    from code_indexer.server.services.dep_map_health_detector import (
        DepMapHealthDetector,
    )

    return DepMapHealthDetector()


def _index_regenerator():
    from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator

    return IndexRegenerator()


def _repair_executor(domain_analyzer=None):
    from code_indexer.server.services.dep_map_repair_executor import (
        DepMapRepairExecutor,
    )

    return DepMapRepairExecutor(
        health_detector=_health_detector(),
        index_regenerator=_index_regenerator(),
        domain_analyzer=domain_analyzer,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fixture builders
# ─────────────────────────────────────────────────────────────────────────────


def _make_domain_md(
    output_dir: Path,
    domain_name: str,
    description: str = "",
    include_frontmatter_desc: bool = True,
) -> Path:
    """
    Write a domain .md file with required sections.

    When include_frontmatter_desc is True, writes 'description: <value>' in
    the YAML frontmatter. When False, omits the key entirely (old format).
    """
    desc_line = f"description: {description}\n" if include_frontmatter_desc else ""
    content = (
        f"---\n"
        f"domain: {domain_name}\n"
        f"{desc_line}"
        f"last_analyzed: 2026-01-15T10:00:00+00:00\n"
        f"participating_repos:\n"
        f"  - repo-alpha\n"
        f"  - repo-beta\n"
        f"---\n"
        f"\n"
        f"# Domain Analysis: {domain_name}\n"
        f"\n"
        f"## Overview\n"
        f"\n"
        f"This domain covers {domain_name} responsibilities across multiple repos.\n"
        f"The bounded context is well-defined with unidirectional dependencies.\n"
        f"Integration points use the adapter pattern to avoid tight coupling.\n"
        f"All downstream consumers depend only on published interfaces.\n"
        f"\n"
        f"## Repository Roles\n"
        f"\n"
        f"- **repo-alpha**: Primary implementation of {domain_name} business logic.\n"
        f"- **repo-beta**: Secondary consumer that delegates to repo-alpha.\n"
        f"\n"
        f"## Intra-Domain Dependencies\n"
        f"\n"
        f"repo-beta -> repo-alpha via REST API. No circular dependencies.\n"
    )
    path = output_dir / f"{domain_name}.md"
    path.write_text(content)
    return path


def _make_domains_json(
    output_dir: Path,
    domain_names: List[str],
    with_metadata: bool = False,
    broken_names: Optional[Set[str]] = None,
) -> None:
    """
    Write _domains.json as the single JSON fixture helper for all tests.

    Modes:
    - with_metadata=False, broken_names=None:
        All domains get empty description/evidence (the fully drifted state).
    - with_metadata=True, broken_names=None:
        All domains get pre-filled description/evidence (the healthy state).
    - broken_names=<set>:
        Domains in broken_names get empty metadata; all others get pre-filled.
        with_metadata is ignored when broken_names is provided.
        Used by test F to create a mixed healthy+broken scenario.
    """
    domains = []
    for name in domain_names:
        is_broken = broken_names is not None and name in broken_names
        use_metadata = (with_metadata and broken_names is None) or (
            broken_names is not None and not is_broken
        )
        domains.append(
            {
                "name": name,
                "description": f"Existing description for {name}"
                if use_metadata
                else "",
                "participating_repos": ["repo-alpha", "repo-beta"],
                "evidence": f"Existing evidence for {name}" if use_metadata else "",
            }
        )
    (output_dir / "_domains.json").write_text(json.dumps(domains, indent=2))


def _load_domains_json(output_dir: Path) -> List[Dict[str, Any]]:
    return json.loads((output_dir / "_domains.json").read_text())


# ─────────────────────────────────────────────────────────────────────────────
# Table parsing helpers
# ─────────────────────────────────────────────────────────────────────────────


def _parse_table_row_cells(line: str) -> List[str]:
    """
    Parse a markdown table row preserving empty cells by position.

    '| auth-domain |  | 2 |' -> ['auth-domain', '', '2']

    Preserving structure lets callers check whether the description column
    (index 1) is empty — filtering-out empty strings would collapse positions.
    """
    inner = line.strip()
    if not inner.startswith("|"):
        return []
    inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split("|")]


def _domain_catalog_rows(index_content: str) -> List[Tuple[str, str]]:
    """Extract (domain_name, description) pairs from the Domain Catalog table."""
    result: List[Tuple[str, str]] = []
    in_catalog = False
    for line in index_content.splitlines():
        stripped = line.strip()
        if stripped == "## Domain Catalog":
            in_catalog = True
            continue
        if in_catalog and stripped.startswith("##"):
            break
        if (
            not in_catalog
            or not stripped.startswith("|")
            or stripped.startswith("|---")
        ):
            continue
        cells = _parse_table_row_cells(line)
        if cells and cells[0] not in ("Domain", ""):
            desc = cells[1] if len(cells) > 1 else ""
            result.append((cells[0], desc))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Test A: Production incident — 6 domains with empty JSON metadata
# ─────────────────────────────────────────────────────────────────────────────

SIX_DOMAINS = [
    "auth-domain",
    "data-pipeline",
    "api-gateway",
    "notification-svc",
    "billing-core",
    "reporting-module",
]


def test_A_health_detector_flags_empty_json_metadata(tmp_path):
    """Check 8 must flag domains with empty JSON metadata when .md files are valid."""
    for name in SIX_DOMAINS:
        _make_domain_md(tmp_path, name, description=f"Description for {name}")
    _make_domains_json(tmp_path, SIX_DOMAINS, with_metadata=False)
    make_index_md(tmp_path)

    report = _health_detector().detect(tmp_path)

    assert report.status != "healthy", (
        "Expected non-healthy when JSON metadata is empty"
    )
    empty_meta = [a for a in report.anomalies if a.type == "empty_json_metadata"]
    assert len(empty_meta) > 0, (
        f"Expected empty_json_metadata anomaly; got: {[a.type for a in report.anomalies]}"
    )

    flagged: set = set()
    for a in empty_meta:
        if a.domain:
            flagged.add(a.domain)
        if a.missing_repos:
            flagged.update(a.missing_repos)
    assert flagged & set(SIX_DOMAINS), (
        f"empty_json_metadata must reference domain names; got {flagged}"
    )


def test_A_healthy_metadata_not_flagged(tmp_path):
    """Check 8 must NOT flag domains that already have non-empty metadata."""
    names = ["auth-domain", "data-pipeline"]
    for name in names:
        _make_domain_md(tmp_path, name, description=f"Good description for {name}")
    _make_domains_json(tmp_path, names, with_metadata=True)
    make_index_md(tmp_path)

    report = _health_detector().detect(tmp_path)
    empty_meta = [a for a in report.anomalies if a.type == "empty_json_metadata"]
    assert len(empty_meta) == 0, f"Must not flag healthy metadata; got: {empty_meta}"


def test_A_empty_json_metadata_not_in_repairable_types():
    """empty_json_metadata must NOT be in REPAIRABLE_ANOMALY_TYPES."""
    from code_indexer.server.services.dep_map_health_detector import (
        REPAIRABLE_ANOMALY_TYPES,
    )

    assert "empty_json_metadata" not in REPAIRABLE_ANOMALY_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# Test B: Incremental new-domain creation
# ─────────────────────────────────────────────────────────────────────────────


def test_B_incremental_new_domain_not_silently_empty(tmp_path):
    """New domain entry must have non-empty description OR needs_reanalysis=True.

    Fix 2 extracts the domain-dict creation block from _discover_and_assign_new_repos
    into _apply_domain_assignments so the assignment logic is independently testable
    without requiring Claude CLI. We call that extracted method directly.
    """
    from code_indexer.server.services.dependency_map_service import DependencyMapService

    service = DependencyMapService.__new__(DependencyMapService)
    existing_domain_list = [
        {
            "name": "existing-domain",
            "description": "An existing domain",
            "participating_repos": ["repo-alpha"],
            "evidence": "existing evidence",
        }
    ]
    # repo-new maps to "new-domain" which does not yet exist in domain_list
    assignments = [{"repo": "repo-new", "domains": ["new-domain"]}]

    # _apply_domain_assignments is extracted by Fix 2 from _discover_and_assign_new_repos.
    # Direct call intentional: tests the specific dict-construction step that previously
    # wrote description="" silently, with no equivalent public API.
    service._apply_domain_assignments(  # type: ignore[attr-defined]  # Fix 2: tests extracted private method
        assignments=assignments,
        domain_list=existing_domain_list,
        dependency_map_dir=tmp_path,
    )

    updated = _load_domains_json(tmp_path)
    new_entries = [d for d in updated if d.get("name") == "new-domain"]
    assert len(new_entries) == 1, "New domain entry must be created"

    entry = new_entries[0]
    assert entry.get("description", "") != "" or entry.get("needs_reanalysis", False), (
        f"New domain must have non-empty description or needs_reanalysis=True. "
        f"Got: description={entry.get('description')!r}, "
        f"needs_reanalysis={entry.get('needs_reanalysis', False)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test C: Phase 3.5 backfill
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("include_frontmatter_desc", [True, False])
def test_C_phase35_backfills_json_description(tmp_path, include_frontmatter_desc):
    """
    Phase 3.5 must populate description from frontmatter (True) or ## Overview (False).
    Both cases must result in non-empty description in _domains.json after repair.
    """
    name = "test-domain"
    _make_domain_md(
        tmp_path,
        name,
        description="Domain description text",
        include_frontmatter_desc=include_frontmatter_desc,
    )
    _make_domains_json(tmp_path, [name], with_metadata=False)
    make_index_md(tmp_path)

    report = _health_detector().detect(tmp_path)
    _repair_executor().execute(tmp_path, report)

    by_name = {d["name"]: d for d in _load_domains_json(tmp_path)}
    assert by_name[name]["description"] != "", (
        f"Phase 3.5 must populate description "
        f"(include_frontmatter_desc={include_frontmatter_desc})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test D: Orphan .md files get JSON entries with backfilled description
# ─────────────────────────────────────────────────────────────────────────────


def test_D_reconcile_then_phase35_backfills_description(tmp_path):
    """
    Phase 3 (_reconcile_domains_json) adds minimal JSON entries for .md files that
    have no JSON record. Phase 3.5 must then backfill the exact description value
    from the .md frontmatter into those entries.

    We call _reconcile_domains_json directly because the full repair flow runs
    Phase 2 first (which would remove the orphan .md before Phase 3 can track it).
    Testing Phase 3 + Phase 3.5 in sequence directly is the correct unit boundary.
    """
    expected_description = "Untracked domain description"
    _make_domain_md(tmp_path, "untracked-domain", description=expected_description)
    # _domains.json has an entry for other-domain but NOT for untracked-domain
    _make_domains_json(tmp_path, ["other-domain"], with_metadata=True)
    _make_domain_md(tmp_path, "other-domain", description="Other domain description")
    make_index_md(tmp_path)

    executor = _repair_executor()

    # Step 1: reconcile adds a minimal entry for untracked-domain.
    # _reconcile_domains_json is the Phase 3 private method of DepMapRepairExecutor.
    # Direct call intentional: isolates Phase 3 from Phase 2, which would otherwise
    # delete the .md file before Phase 3 can track it.
    executor._reconcile_domains_json(tmp_path)  # type: ignore[attr-defined]  # Phase 3: private method under test

    after_reconcile = _load_domains_json(tmp_path)
    untracked = [d for d in after_reconcile if d.get("name") == "untracked-domain"]
    assert len(untracked) == 1, (
        "_reconcile_domains_json must add a JSON entry for untracked-domain"
    )
    assert untracked[0].get("description", "") == "", (
        "Entry added by reconcile must start with empty description (minimal entry)"
    )

    # Step 2: Phase 3.5 must backfill the exact frontmatter description.
    report = _health_detector().detect(tmp_path)
    executor.execute(tmp_path, report)

    updated = _load_domains_json(tmp_path)
    by_name = {d["name"]: d for d in updated}
    assert "untracked-domain" in by_name, (
        "untracked-domain must remain in JSON after repair"
    )
    assert by_name["untracked-domain"].get("description", "") == expected_description, (
        f"Phase 3.5 must backfill exact frontmatter description. "
        f"Expected: {expected_description!r}, "
        f"Got: {by_name['untracked-domain'].get('description', '')!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test E: Idempotency
# ─────────────────────────────────────────────────────────────────────────────


def test_E_repair_idempotent(tmp_path):
    """Two consecutive repair runs must produce identical _domains.json state."""
    names = ["auth-domain", "data-pipeline"]
    for name in names:
        _make_domain_md(tmp_path, name, description=f"Description for {name}")
    _make_domains_json(tmp_path, names, with_metadata=False)
    make_index_md(tmp_path)

    executor = _repair_executor()

    report1 = _health_detector().detect(tmp_path)
    executor.execute(tmp_path, report1)
    state1 = _load_domains_json(tmp_path)

    report2 = _health_detector().detect(tmp_path)
    executor.execute(tmp_path, report2)
    state2 = _load_domains_json(tmp_path)

    assert state1 == state2, (
        f"Repair must be idempotent.\nAfter run 1: {state1}\nAfter run 2: {state2}"
    )
    by_name = {d["name"]: d for d in state2}
    for name in names:
        assert by_name[name]["description"] != "", (
            f"Domain '{name}' must retain non-empty description after second run"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test F: Partial drift
# ─────────────────────────────────────────────────────────────────────────────


def test_F_only_broken_domains_updated(tmp_path):
    """Phase 3.5 must update only drifted domains; healthy ones stay unchanged."""
    healthy = ["auth-domain", "billing-core"]
    broken = ["data-pipeline", "api-gateway"]
    all_names = healthy + broken

    for name in all_names:
        _make_domain_md(tmp_path, name, description=f"Md desc for {name}")

    # broken_names controls which entries get empty metadata vs pre-filled
    _make_domains_json(tmp_path, all_names, broken_names=set(broken))
    make_index_md(tmp_path)

    report = _health_detector().detect(tmp_path)
    _repair_executor().execute(tmp_path, report)

    by_name = {d["name"]: d for d in _load_domains_json(tmp_path)}

    for name in healthy:
        assert by_name[name]["description"] == f"Existing description for {name}", (
            f"Healthy domain '{name}' description must be unchanged"
        )
        assert by_name[name]["evidence"] == f"Existing evidence for {name}", (
            f"Healthy domain '{name}' evidence must be unchanged"
        )
    for name in broken:
        assert by_name[name]["description"] != "", (
            f"Broken domain '{name}' must have non-empty description after repair"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test G: Pass 2 frontmatter + index body
# ─────────────────────────────────────────────────────────────────────────────


def test_G_pass2_frontmatter_builder_includes_description():
    """
    Fix 1: DependencyMapAnalyzer._build_domain_frontmatter must include 'description'.
    We call the real production method directly on the real class instance.
    """
    from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
    from code_indexer.server.services.dep_map_file_utils import parse_yaml_frontmatter

    analyzer = DependencyMapAnalyzer.__new__(DependencyMapAnalyzer)

    # _build_domain_frontmatter is added by Fix 1 to make the frontmatter-building
    # logic in run_pass_2_per_domain independently testable. Direct call is
    # intentional: we are testing this private extraction as the fix itself.
    fm_text = analyzer._build_domain_frontmatter(  # type: ignore[attr-defined]  # Fix 1: private method under test
        domain_name="auth-domain",
        description="Handles all authentication flows",
        participating_repos=["repo-alpha", "repo-beta"],
    )

    parsed = parse_yaml_frontmatter(fm_text + "\n## Overview\n\nBody.\n")
    assert parsed is not None, (
        "_build_domain_frontmatter must produce parseable frontmatter"
    )
    assert "description" in parsed, (
        f"Pass 2 frontmatter must include 'description'. Keys: {list(parsed.keys())}"
    )
    assert parsed["description"] == "Handles all authentication flows", (
        f"description mismatch: got {parsed.get('description')!r}"
    )


def test_G_index_catalog_uses_frontmatter_desc_when_json_empty(tmp_path):
    """Fix 6: Domain Catalog rows must use frontmatter description when JSON is empty."""
    names = ["auth-domain", "data-pipeline"]
    for name in names:
        _make_domain_md(tmp_path, name, description=f"{name} frontmatter description")
    _make_domains_json(tmp_path, names, with_metadata=False)

    _index_regenerator().regenerate(tmp_path)

    rows = _domain_catalog_rows((tmp_path / "_index.md").read_text())
    assert len(rows) > 0, "Domain Catalog must have data rows"

    for domain_name, desc_cell in rows:
        if domain_name in names:
            assert desc_cell != "", (
                f"Row for '{domain_name}' must have non-empty description; "
                f"got empty cell (frontmatter fallback not applied)"
            )


def test_G_full_pipeline_no_empty_catalog_cells(tmp_path):
    """Full pipeline: repair + index regen must leave no empty description cells."""
    names = ["auth-domain", "data-pipeline", "api-gateway"]
    for name in names:
        _make_domain_md(tmp_path, name, description=f"Description of {name}")
    _make_domains_json(tmp_path, names, with_metadata=False)

    report = _health_detector().detect(tmp_path)
    _repair_executor().execute(tmp_path, report)

    index_file = tmp_path / "_index.md"
    assert index_file.exists(), "_index.md must be generated"

    for domain_name, desc_cell in _domain_catalog_rows(index_file.read_text()):
        if domain_name in names:
            assert desc_cell != "", (
                f"Domain Catalog row for '{domain_name}' must have non-empty "
                f"description after repair. Got empty cell."
            )
