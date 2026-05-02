"""
Shared fixture helpers for Story #889 tests.

Consumed by:
  test_dep_map_889_ac3_shape.py
  test_dep_map_889_ac4_reuse.py
  test_dep_map_889_ac5_resolution.py
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


def _make_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    return user


def _make_app_state(read_path: Path) -> MagicMock:
    state = MagicMock()
    state.dependency_map_service.cidx_meta_read_path = read_path
    return state


def _parse_response(result: Any) -> Dict[str, Any]:
    # cast needed: json.loads() returns Any; caller contracts guarantee dict shape
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _call_hub(params: dict, root: Path) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers.depmap import depmap_get_hub_domains_handler

    state = _make_app_state(root)
    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        state,
    ):
        result = depmap_get_hub_domains_handler(params, _make_user())
    return _parse_response(result)


def _write_domain_md(
    dep_map_dir: Path,
    domain: str,
    outgoing: Optional[List[Dict[str, str]]] = None,
    incoming: Optional[List[Dict[str, str]]] = None,
) -> None:
    """Write a minimal domain markdown file with optional outgoing/incoming rows."""
    out_rows = "".join(
        f"| {r['this_repo']} | {r['depends_on']} | {r['target_domain']} "
        f"| {r.get('dep_type', 'Code-level')} | why | ev |\n"
        for r in (outgoing or [])
    )
    in_rows = "".join(
        f"| {r['external_repo']} | {r['depends_on']} | {r['source_domain']} "
        "| Code-level | why | ev |\n"
        for r in (incoming or [])
    )
    content = (
        f"---\nname: {domain}\n---\n"
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


def _make_empty_graph(tmp_path: Path) -> Path:
    """Dep-map with no domains — hub tool returns empty list."""
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    (dep_map_dir / "_domains.json").write_text(json.dumps([]), encoding="utf-8")
    return tmp_path


def _make_hub_graph(tmp_path: Path) -> Path:
    """Five-domain hub graph with known degree values.

    Edges: alpha->beta, alpha->gamma, alpha->delta, beta->delta, gamma->delta, delta->epsilon

    Degree summary:
      domain   | out | in | total
      alpha    |  3  |  0 |  3
      beta     |  1  |  1 |  2
      gamma    |  1  |  1 |  2
      delta    |  1  |  3 |  4    <- highest total and in_degree
      epsilon  |  0  |  1 |  1
    """
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    domains = [
        {"name": d, "description": "d", "participating_repos": []}
        for d in ["alpha", "beta", "gamma", "delta", "epsilon"]
    ]
    (dep_map_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    _write_domain_md(
        dep_map_dir,
        "alpha",
        outgoing=[
            {
                "this_repo": "r1",
                "depends_on": "r2",
                "target_domain": "beta",
                "dep_type": "Code-level",
            },
            {
                "this_repo": "r1",
                "depends_on": "r3",
                "target_domain": "gamma",
                "dep_type": "Code-level",
            },
            {
                "this_repo": "r1",
                "depends_on": "r4",
                "target_domain": "delta",
                "dep_type": "Code-level",
            },
        ],
    )
    _write_domain_md(
        dep_map_dir,
        "beta",
        outgoing=[
            {
                "this_repo": "r2",
                "depends_on": "r4",
                "target_domain": "delta",
                "dep_type": "Code-level",
            }
        ],
        incoming=[
            {"external_repo": "r1", "depends_on": "r2", "source_domain": "alpha"}
        ],
    )
    _write_domain_md(
        dep_map_dir,
        "gamma",
        outgoing=[
            {
                "this_repo": "r3",
                "depends_on": "r4",
                "target_domain": "delta",
                "dep_type": "Code-level",
            }
        ],
        incoming=[
            {"external_repo": "r1", "depends_on": "r3", "source_domain": "alpha"}
        ],
    )
    _write_domain_md(
        dep_map_dir,
        "delta",
        outgoing=[
            {
                "this_repo": "r4",
                "depends_on": "r5",
                "target_domain": "epsilon",
                "dep_type": "Code-level",
            }
        ],
        incoming=[
            {"external_repo": "r1", "depends_on": "r4", "source_domain": "alpha"},
            {"external_repo": "r2", "depends_on": "r4", "source_domain": "beta"},
            {"external_repo": "r3", "depends_on": "r4", "source_domain": "gamma"},
        ],
    )
    _write_domain_md(
        dep_map_dir,
        "epsilon",
        incoming=[
            {"external_repo": "r4", "depends_on": "r5", "source_domain": "delta"}
        ],
    )
    return tmp_path
