"""
Shared fixture helpers for Story #887 tests.

All AC test files import from here to avoid duplication.
No test classes — only helper functions.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# Safe domain name: alphanumeric, hyphens, underscores, dots — no path separators
_SAFE_DOMAIN_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")


def _validate_dir(dep_map_dir: Path) -> None:
    if dep_map_dir is None:
        raise ValueError("dep_map_dir must not be None")
    if not dep_map_dir.is_dir():
        raise ValueError(
            f"dep_map_dir does not exist or is not a directory: {dep_map_dir}"
        )


def _validate_domain_name(domain_name: str) -> None:
    if not domain_name:
        raise ValueError("domain_name must not be empty")
    if not _SAFE_DOMAIN_RE.match(domain_name):
        raise ValueError(
            f"domain_name contains unsafe characters for filesystem use: {domain_name!r}"
        )
    # Secondary containment check handled by callers via .resolve().relative_to()


def make_parser(path: Path):
    """Construct a DepMapMCPParser with lazy import."""
    if path is None:
        raise ValueError("path must not be None")
    from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser

    return DepMapMCPParser(path)


def import_hygiene_symbol(name: str) -> Any:
    """Lazy-import a single symbol from dep_map_parser_hygiene by name."""
    if not name:
        raise ValueError("name must not be empty")
    from code_indexer.server.services import dep_map_parser_hygiene

    return getattr(dep_map_parser_hygiene, name)


def write_domains_json(dep_map_dir: Path, domains: List[Dict]) -> None:
    _validate_dir(dep_map_dir)
    if domains is None:
        raise ValueError("domains must not be None")
    (dep_map_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")


def write_domain_md_graph(
    dep_map_dir: Path,
    domain_name: str,
    outgoing_rows: Optional[List[Dict]] = None,
    incoming_rows: Optional[List[Dict]] = None,
) -> None:
    """Write a minimal domain .md for graph tests with path-traversal protection."""
    _validate_dir(dep_map_dir)
    _validate_domain_name(domain_name)
    if outgoing_rows is None:
        outgoing_rows = []
    if incoming_rows is None:
        incoming_rows = []

    # Secondary containment: resolve and confirm destination stays under dep_map_dir
    dest = (dep_map_dir / f"{domain_name}.md").resolve()
    dest.relative_to(dep_map_dir.resolve())  # raises ValueError if dest escapes base

    frontmatter = f"---\nname: {domain_name}\n---\n"
    out_header = (
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    out_body = "".join(
        f"| {r['this_repo']} | {r['depends_on']} | {r['target_domain']} | "
        f"{r.get('dep_type', 'Code-level')} | why | evidence |\n"
        for r in outgoing_rows
    )
    in_header = (
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    in_body = "".join(
        f"| {r['external_repo']} | {r['depends_on']} | {r['source_domain']} | "
        f"{r.get('dep_type', 'Code-level')} | why | evidence |\n"
        for r in incoming_rows
    )
    body = (
        f"# Domain Analysis: {domain_name}\n\n"
        "## Cross-Domain Connections\n\n"
        + out_header
        + out_body
        + "\n"
        + in_header
        + in_body
    )
    dest.write_text(frontmatter + body, encoding="utf-8")


def make_simple_graph(
    dep_map_root: Path,
    src: str,
    tgt: str,
    dep_type: str = "Code-level",
    bidirectional: bool = True,
) -> None:
    """Create a 2-domain graph with src→tgt edge, optionally confirmed bidirectionally."""
    if dep_map_root is None:
        raise ValueError("dep_map_root must not be None")
    _validate_domain_name(src)
    _validate_domain_name(tgt)
    d = dep_map_root / "dependency-map"
    write_domains_json(
        d,
        [
            {"name": src, "description": "d", "participating_repos": []},
            {"name": tgt, "description": "d", "participating_repos": []},
        ],
    )
    write_domain_md_graph(
        d,
        src,
        outgoing_rows=[
            {
                "this_repo": "repo-s",
                "depends_on": "repo-t",
                "target_domain": tgt,
                "dep_type": dep_type,
            }
        ],
    )
    incoming = (
        [
            {
                "external_repo": "repo-s",
                "depends_on": "repo-t",
                "source_domain": src,
                "dep_type": dep_type,
            }
        ]
        if bidirectional
        else []
    )
    write_domain_md_graph(d, tgt, incoming_rows=incoming)


def make_self_loop_graph(dep_map_root: Path, domain: str) -> None:
    """Create a domain with a self-loop outgoing→incoming edge."""
    if dep_map_root is None:
        raise ValueError("dep_map_root must not be None")
    _validate_domain_name(domain)
    d = dep_map_root / "dependency-map"
    write_domains_json(
        d, [{"name": domain, "description": "d", "participating_repos": []}]
    )
    write_domain_md_graph(
        d,
        domain,
        outgoing_rows=[
            {
                "this_repo": "repo-x",
                "depends_on": "repo-y",
                "target_domain": domain,
                "dep_type": "Code-level",
            }
        ],
        incoming_rows=[
            {
                "external_repo": "repo-x",
                "depends_on": "repo-y",
                "source_domain": domain,
                "dep_type": "Code-level",
            }
        ],
    )
