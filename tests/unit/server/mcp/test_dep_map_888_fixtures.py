"""
Shared fixture helpers for Story #888 tests.

Provides _DepMapDir builder (3-method class), _write_domain_md (with inlined
path guard), _call_handler, _assert_resolution, and named fixture factories.
Consumed by test_dep_map_888_ac*.py files.
"""

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


def _call_handler(handler: Callable, params: dict, root: Path) -> Dict[str, Any]:
    """Call a depmap handler with a fresh mock user and app state rooted at root.

    Returns the parsed inner response dict (not the MCP envelope).
    """
    user = MagicMock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    state = MagicMock()
    state.dependency_map_service.cidx_meta_read_path = root
    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        state,
    ):
        result = handler(params, user)
    # cast needed: json.loads() returns Any; MCP handlers always return dict envelope
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _assert_resolution(
    data: Dict[str, Any],
    expected_resolution: str,
    expected_success: bool,
    context: str = "",
) -> None:
    """Assert resolution and success fields match expected values."""
    prefix = f"{context}: " if context else ""
    assert data.get("resolution") == expected_resolution, (
        f"{prefix}Expected resolution={expected_resolution!r}, "
        f"got: {data.get('resolution')!r}"
    )
    assert data.get("success") is expected_success, (
        f"{prefix}Expected success={expected_success}, got: {data.get('success')!r}"
    )


class _DepMapDir:
    """Minimal dep-map directory builder."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.dir = tmp_path / "dependency-map"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._domains: list = []

    def add_domain(
        self, name: str, repos: list, description: str = "d"
    ) -> "_DepMapDir":
        """Register a domain entry (written on flush)."""
        self._domains.append(
            {"name": name, "description": description, "participating_repos": repos}
        )
        return self

    def flush(self) -> "_DepMapDir":
        """Write _domains.json from registered entries."""
        (self.dir / "_domains.json").write_text(
            json.dumps(self._domains), encoding="utf-8"
        )
        return self


def _write_domain_md(
    dep_map_dir: Path,
    domain: str,
    roles: Optional[Dict[str, str]] = None,
    outgoing: Optional[List] = None,
    incoming: Optional[List] = None,
) -> None:
    """Write a domain markdown file into dep_map_dir.

    Raises ValueError if domain contains path-unsafe characters.
    """
    if not re.match(r"^[A-Za-z0-9_\-]+$", domain):
        raise ValueError(f"domain name {domain!r} contains path-unsafe characters")

    roles_section = ""
    if roles:
        rows = "".join(f"| {r} | Python | {rl} |\n" for r, rl in roles.items())
        roles_section = (
            "## Repository Roles\n\n| Repository | Language | Role |\n|---|---|---|\n"
            + rows
            + "\n"
        )

    out_rows = "".join(
        f"| {r['this_repo']} | {r['depends_on']} | {r['target_domain']} "
        "| Code-level | why | ev |\n"
        for r in (outgoing or [])
    )
    in_rows = "".join(
        f"| {r['external_repo']} | {r['depends_on']} | {r['source_domain']} "
        "| Code-level | why | ev |\n"
        for r in (incoming or [])
    )
    content = (
        f"---\nname: {domain}\n---\n"
        f"{roles_section}"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        f"{out_rows}"
        "\n### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        f"{in_rows}"
    )
    (dep_map_dir / f"{domain}.md").write_text(content, encoding="utf-8")


def make_empty_dep_map(tmp_path: Path) -> Path:
    """Empty dep-map (no domains). Returns root path."""
    return _DepMapDir(tmp_path).flush().root


def make_dep_map_with_consumer(tmp_path: Path, repo_name: str) -> Path:
    """Dep-map where consumer-repo depends on repo_name (ok path for find_consumers)."""
    consumer = "consumer-repo"
    domain = "alpha-domain"
    d = _DepMapDir(tmp_path).add_domain(domain, repos=[repo_name, consumer]).flush()
    _write_domain_md(
        d.dir,
        domain,
        incoming=[
            {
                "external_repo": consumer,
                "depends_on": repo_name,
                "source_domain": domain,
            }
        ],
    )
    return d.root


def make_dep_map_indexed_no_consumers(tmp_path: Path, repo_name: str) -> Path:
    """Dep-map where repo_name is indexed but nobody depends on it."""
    domain = "isolated-domain"
    d = _DepMapDir(tmp_path).add_domain(domain, repos=[repo_name]).flush()
    _write_domain_md(d.dir, domain)
    return d.root


def make_dep_map_with_domain(tmp_path: Path, domain_name: str, repo_name: str) -> Path:
    """Dep-map with domain_name indexed and repo_name as a participant with a role."""
    d = _DepMapDir(tmp_path).add_domain(domain_name, repos=[repo_name]).flush()
    _write_domain_md(d.dir, domain_name, roles={repo_name: "Core service"})
    return d.root


def make_two_domain_graph(tmp_path: Path) -> Path:
    """Two-domain graph: src-dom -> tgt-dom with bidirectional confirmation."""
    d = (
        _DepMapDir(tmp_path)
        .add_domain("src-dom", repos=[])
        .add_domain("tgt-dom", repos=[])
        .flush()
    )
    _write_domain_md(
        d.dir,
        "src-dom",
        outgoing=[
            {"this_repo": "repo-s", "depends_on": "repo-t", "target_domain": "tgt-dom"}
        ],
    )
    _write_domain_md(
        d.dir,
        "tgt-dom",
        incoming=[
            {
                "external_repo": "repo-s",
                "depends_on": "repo-t",
                "source_domain": "src-dom",
            }
        ],
    )
    return d.root
