"""Shared neutral fixture for cidx-meta visibility tests.

Builds a REAL cidx-meta directory on disk and REAL group rows:

- ``alpha`` is granted to the test user's group;
- ``beta-team-sample`` is granted to another group (the canary);
- ``gamma`` is granted to no group;
- ``dependency-map/`` holds a ``payments`` domain (alpha and
  beta-team-sample, with an alpha -> beta-team-sample outgoing row), a
  standalone ``beta-team-sample`` domain, a ``gamma`` domain, a visible domain
  with a malformed table, a visible domain with unparseable frontmatter, a
  legacy domain without ``participating_repos`` (absent from
  ``_domains.json``), and an ``_index.md`` produced by the real
  ``IndexRegenerator``;
- memories (global, alpha-scoped, beta-scoped), X-Ray patterns, and
  ``.git`` / ``.code-indexer`` content containing the canary.

A real SQLite global-repos registry provides the alias universe.
Every name is neutral sample data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.services.memory_io import atomic_write_memory_file

ALPHA = "alpha"
CANARY = "beta-team-sample"
GAMMA = "gamma"
SHORT_ALIAS = "zed"
UNIVERSE_ONLY_ALIAS = "omega-sample"

RESTRICTED_USER = "fixture-user"
OTHER_USER = "fixture-other"
ADMIN_USER = "fixture-admin"
UNGROUPED_USER = "fixture-ungrouped"

MEMORY_GLOBAL = "a1" * 16
MEMORY_ALPHA = "b2" * 16
MEMORY_CANARY = "c3" * 16

REGISTERED_ALIASES = (
    ALPHA,
    CANARY,
    GAMMA,
    SHORT_ALIAS,
    UNIVERSE_ONLY_ALIAS,
    "cidx-meta",
)

# Line numbers (1-based) of the payments "Integration Notes" table.
PAYMENTS_NOTES_HEADER_LINE = 19
PAYMENTS_NOTES_FIRST_ROW = 21
PAYMENTS_NOTES_ROWS = 40


@dataclass
class CidxMetaFixture:
    root: Path
    dep_map_dir: Path
    group_manager: GroupAccessManager
    access_service: AccessFilteringService
    registry: Any


def _frontmatter(domain: str, repos: List[str], description: str) -> List[str]:
    lines = [
        "---",
        f"domain: {domain}",
        f"description: {description}",
        "last_analyzed: 2026-01-01T00:00:00+00:00",
        "participating_repos:",
    ]
    lines += [f"  - {repo}" for repo in repos]
    lines.append("---")
    return lines


def _cross_domain(outgoing: List[str], incoming: List[str]) -> List[str]:
    return (
        [
            "## Cross-Domain Connections",
            "",
            "### Outgoing Dependencies",
            "",
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |",
            "|---|---|---|---|---|---|",
        ]
        + outgoing
        + [
            "",
            "### Incoming Dependencies",
            "",
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |",
            "|---|---|---|---|---|---|",
        ]
        + incoming
    )


def payments_lines() -> List[str]:
    """The payments domain file, one entry per line."""
    owners = [ALPHA, CANARY, GAMMA, ALPHA]
    notes_rows = []
    for k in range(1, PAYMENTS_NOTES_ROWS + 1):
        note = f"step {k} hands off to {CANARY}" if k % 3 == 0 else f"step {k} local"
        notes_rows.append(f"| {k} | {owners[k % 4]} | {note} |")
    lines = _frontmatter(
        "payments", [ALPHA, CANARY], "Payment processing across services"
    )
    lines += [
        "",
        "# Domain Analysis: payments",
        "",
        "## Overview",
        "",
        f"The payments domain covers {ALPHA} and {CANARY}.",
        f"Stock checks are delegated to {GAMMA} before capture.",
        "",
        "## Integration Notes",
        "",
        "| Step | Owner | Notes |",
        "|---|---|---|",
    ]
    assert len(lines) + 1 == PAYMENTS_NOTES_FIRST_ROW
    lines += notes_rows
    lines += [
        "",
        "## Repository Roles",
        "",
        "| Repository | Language | Role |",
        "|---|---|---|",
        f"| {ALPHA} | Python | Payment API |",
        f"| {CANARY} | Go | Settlement worker |",
        "",
    ]
    lines += _cross_domain(
        [
            f"| {ALPHA} | {CANARY} | {CANARY} | Service integration | settles via queue | src/settle.py |",
            f"| {ALPHA} | {ALPHA} | ledger | Code-level | posts ledger entries | src/ledger.py |",
            f"| {CANARY} | {ALPHA} | ledger | Data contracts | writes journal rows | db/journal.sql |",
        ],
        [
            f"| {GAMMA} | {ALPHA} | inventory | Service integration | reserves stock | api/reserve.py |",
        ],
    )
    return lines


def _domain_file(domain: str, repos: List[str], body: List[str]) -> str:
    lines = _frontmatter(domain, repos, f"{domain} domain") + [""] + body
    return "\n".join(lines) + "\n"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_dependency_map(dep_map_dir: Path) -> None:
    _write(dep_map_dir / "payments.md", "\n".join(payments_lines()) + "\n")
    _write(
        dep_map_dir / "ledger.md",
        _domain_file(
            "ledger",
            [ALPHA],
            [
                "# Domain Analysis: ledger",
                "",
                "## Repository Roles",
                "",
                "| Repository | Language | Role |",
                "|---|---|---|",
                f"| {ALPHA} | Python | Ledger writer |",
                f"| {ALPHA} | Python | Ledger | extra |",
                f"| {CANARY} | Go |",
            ],
        ),
    )
    _write(
        dep_map_dir / f"{CANARY}.md",
        _domain_file(
            CANARY,
            [CANARY],
            [f"# Domain Analysis: {CANARY}", "", f"{CANARY} stands alone."],
        ),
    )
    _write(
        dep_map_dir / "inventory.md",
        _domain_file(
            "inventory",
            [GAMMA],
            ["# Domain Analysis: inventory", "", f"{GAMMA} tracks stock."],
        ),
    )
    _write(
        dep_map_dir / "reports.md",
        "---\ndomain: reports\nparticipating_repos: [alpha, beta-team-sample\n---\n"
        f"\n# Domain Analysis: reports\n\n{ALPHA} renders reports.\n",
    )
    _write(
        dep_map_dir / "legacy.md",
        "---\ndomain: legacy\n---\n\n# Domain Analysis: legacy\n\n"
        f"{ALPHA} once lived here.\n",
    )
    domains: List[Dict[str, Any]] = [
        {
            "name": "payments",
            "description": "Payments",
            "participating_repos": [ALPHA, CANARY],
        },
        {"name": "ledger", "description": "Ledger", "participating_repos": [ALPHA]},
        {
            "name": CANARY,
            "description": f"{CANARY} (standalone repository)",
            "participating_repos": [CANARY],
        },
        {
            "name": "inventory",
            "description": "Inventory",
            "participating_repos": [GAMMA],
        },
        {"name": "reports", "description": "Reports", "participating_repos": [ALPHA]},
    ]
    _write(dep_map_dir / "_domains.json", json.dumps(domains, indent=2))
    _write(dep_map_dir / "_journal.json", json.dumps({"last": CANARY}))
    _write(dep_map_dir / "_activity.md", f"# Activity\n\n{CANARY} refreshed\n")
    _write(dep_map_dir / "_dep_types.md", "# Dependency types\n\nCode-level\n")
    _write(dep_map_dir / "_analysis_guidelines.md", "# Guidelines\n\nBe precise.\n")
    _write(dep_map_dir / "x.jsonl", json.dumps({"repo": CANARY}) + "\n")
    IndexRegenerator().regenerate(dep_map_dir)


def _write_memory(
    memories_dir: Path, memory_id: str, scope: str, referenced_repo: Optional[str]
) -> None:
    memories_dir.mkdir(parents=True, exist_ok=True)
    fm: Dict[str, Any] = {
        "id": memory_id,
        "type": "gotcha",
        "scope": scope,
        "summary": "fixture memory",
        "tags": [],
        "created_by": "tester",
        "created_at": "2026-01-01T00:00:00+00:00",
        "edited_by": None,
        "edited_at": None,
    }
    if referenced_repo is not None:
        fm["referenced_repo"] = referenced_repo
    atomic_write_memory_file(memories_dir / f"{memory_id}.md", fm)


def _write_tree(root: Path) -> None:
    _write(
        root / f"{ALPHA}.md",
        f"# {ALPHA}\n\n{ALPHA} talks to {CANARY} and {GAMMA} over HTTP.\n"
        f"It mirrors {UNIVERSE_ONLY_ALIAS} nightly.\n"
        f"The {SHORT_ALIAS} adapter is optional.\n"
        "alphabet ordering applies.\n",
    )
    _write(
        root / f"{CANARY}.md",
        f"# {CANARY}\n\nUnlike {ALPHA}, it keeps an alphabet of flags.\n",
    )
    _write(root / f"{GAMMA}.md", f"# {GAMMA}\n\nInventory service.\n")
    _write(root / "README.md", f"# Catalog\n\nLists {CANARY} and {ALPHA}.\n")
    _write(root / ".gitignore", "*.tmp\n")
    _write(root / ".self-check-probe", "probe\n")
    _write(root / ".git" / "config", f"[remote] {CANARY}\n")
    _write(root / ".code-indexer" / "index" / "meta.json", json.dumps([CANARY]))
    _write(root / "xray-patterns" / "__any__" / "p.yaml", "name: p\n")
    _write(root / "xray-patterns" / CANARY / "p.yaml", f"name: {CANARY}\n")
    memories = root / "memories"
    _write_memory(memories, MEMORY_GLOBAL, "global", None)
    _write_memory(memories, MEMORY_ALPHA, "repo", f"{ALPHA}-global")
    _write_memory(memories, MEMORY_CANARY, "repo", f"{CANARY}-global")
    _write_dependency_map(root / "dependency-map")


def _build_groups(db_path: Path) -> GroupAccessManager:
    gam = GroupAccessManager(db_path)
    team = gam.create_group("fixture-team", "restricted test group")
    other = gam.create_group("other-team", "canary owner group")
    gam.grant_repo_access(ALPHA, team.id, granted_by="test")
    gam.grant_repo_access(CANARY, other.id, granted_by="test")
    gam.assign_user_to_group(RESTRICTED_USER, team.id, assigned_by="test")
    gam.assign_user_to_group(OTHER_USER, other.id, assigned_by="test")
    admins = gam.get_group_by_name("admins")
    assert admins is not None
    gam.assign_user_to_group(ADMIN_USER, admins.id, assigned_by="test")
    return gam


def _build_registry(db_path: Path) -> Any:
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import GlobalReposSqliteBackend

    DatabaseSchema(str(db_path)).initialize_database()
    registry = GlobalReposSqliteBackend(str(db_path))
    for alias in REGISTERED_ALIASES:
        registry.register_repo(
            alias_name=f"{alias}-global",
            repo_name=alias,
            repo_url=None,
            index_path=f"/golden/{alias}",
        )
    return registry


def build_cidx_meta_fixture(tmp_path: Path) -> CidxMetaFixture:
    """Create the cidx-meta tree, the group store and the registry."""
    root = tmp_path / "cidx-meta"
    _write_tree(root)
    gam = _build_groups(tmp_path / "groups.db")
    service = AccessFilteringService(gam, memories_dir=root / "memories")
    registry = _build_registry(tmp_path / "registry.db")
    return CidxMetaFixture(
        root=root,
        dep_map_dir=root / "dependency-map",
        group_manager=gam,
        access_service=service,
        registry=registry,
    )


def read_lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8").splitlines()


# Opt-in extension (depmap tools, Web dependency map, quick reference).
AUDIT_DOMAIN = "audit"
LANGFUSE_GRANTED = "langfuse_sample_granted"
LANGFUSE_HIDDEN = "langfuse_sample_hidden"


def _audit_lines() -> List[str]:
    lines = _frontmatter(AUDIT_DOMAIN, [ALPHA, CANARY], "Audit trail")
    lines += [
        "",
        f"# Domain Analysis: {AUDIT_DOMAIN}",
        "",
        "## Repository Roles",
        "",
        "| Repository | Language | Role |",
        "|---|---|---|",
        f"| {ALPHA} | Python | Audit reader |",
        f"| {CANARY} | Go | Audit writer |",
        "",
    ]
    lines += _cross_domain(
        [
            f"| {CANARY} | {CANARY} | payments | Service integration | audits settlements | src/audit.py |",
        ],
        [
            f"| {ALPHA} | {ALPHA} | ledger | Code-level | reads audit rows | src/read.py |",
            f"| {CANARY} | {ALPHA} | payments | Data contracts | streams audit rows | src/stream.py |",
        ],
    )
    return lines


def add_depmap_extension(fx: CidxMetaFixture) -> None:
    """Add the ``audit`` domain (alpha and the canary; its only edge, to
    payments, runs through the canary; alpha has one visible and one hidden
    consumer there) and two Langfuse trace aliases, one granted to the
    restricted user's group and one granted to nobody."""
    _write(fx.dep_map_dir / f"{AUDIT_DOMAIN}.md", "\n".join(_audit_lines()) + "\n")
    domains_file = fx.dep_map_dir / "_domains.json"
    domains = json.loads(domains_file.read_text(encoding="utf-8"))
    domains.append(
        {
            "name": AUDIT_DOMAIN,
            "description": f"Audit trail written by {CANARY}",
            "participating_repos": [ALPHA, CANARY],
        }
    )
    _write(domains_file, json.dumps(domains, indent=2))
    IndexRegenerator().regenerate(fx.dep_map_dir)
    for alias in (LANGFUSE_GRANTED, LANGFUSE_HIDDEN):
        fx.registry.register_repo(
            alias_name=f"{alias}-global",
            repo_name=alias,
            repo_url=None,
            index_path=f"/golden/{alias}",
        )
    team = fx.group_manager.get_group_by_name("fixture-team")
    assert team is not None
    fx.group_manager.grant_repo_access(LANGFUSE_GRANTED, team.id, granted_by="test")
