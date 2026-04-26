"""
Story #889 — Blocker 1: Graph filter input validation.

Extends the invalid_input resolution contract (Story #888) to cover
graph filter params in depmap_get_cross_domain_graph.

Unsafe cases from Codex review (source_domain / target_domain):
  - int                          → raw TypeError: 'int' object is not iterable
  - dict                         → silently reinterpreted as frozenset of keys
  - tuple                        → silently accepted
  - set                          → silently accepted
  - bool                         → silently accepted
  - list[str, non-str]           → silently accepted (partial bad element)

Unsafe cases (min_count):
  - 0                            → full graph returned (schema says minimum: 1)
  - -1                           → full graph returned
  - string "3"                   → silently treated as None
  - float 2.5                    → silently treated as None
  - bool True/False              → bool is subclass of int, must be rejected

All cases MUST produce success=false, resolution="invalid_input" with
a non-empty error field.

Valid inputs (str, list[str], None, valid positive int) must remain accepted.
Empty params must return full graph (backward-compat).
"""

import json
from pathlib import Path
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    # cast needed: json.loads() returns Any; MCP handlers always return dict envelope
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _call_graph(params: dict, root: Path) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers.depmap import (
        depmap_get_cross_domain_graph_handler,
    )

    state = _make_app_state(root)
    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        state,
    ):
        result = depmap_get_cross_domain_graph_handler(params, _make_user())
    return _parse_response(result)


def _make_minimal_graph(tmp_path: Path) -> Path:
    """Minimal valid dep-map with one edge: alpha -> beta."""
    import json as _json

    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    domains = [
        {"name": "alpha", "description": "d", "participating_repos": []},
        {"name": "beta", "description": "d", "participating_repos": []},
    ]
    (dep_map_dir / "_domains.json").write_text(_json.dumps(domains), encoding="utf-8")

    (dep_map_dir / "alpha.md").write_text(
        "---\nname: alpha\n---\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| r1 | r2 | beta | Code-level | why | ev |\n"
        "\n### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n",
        encoding="utf-8",
    )
    (dep_map_dir / "beta.md").write_text(
        "---\nname: beta\n---\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "\n### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| r1 | r2 | alpha | Code-level | why | ev |\n",
        encoding="utf-8",
    )
    return tmp_path


def _assert_invalid_input(
    param_name: str, bad_value: Any, root: Path
) -> Dict[str, Any]:
    """Call the handler with one invalid param, assert the full invalid_input contract,
    and return the response for further assertions by the caller."""
    data = _call_graph({param_name: bad_value}, root)
    assert data["success"] is False, (
        f"Expected success=false for {param_name}={bad_value!r}, "
        f"got success={data['success']}"
    )
    assert data["resolution"] == "invalid_input", (
        f"Expected resolution='invalid_input' for {param_name}={bad_value!r}, "
        f"got resolution={data['resolution']!r}"
    )
    assert data.get("edges") == [], (
        f"Expected empty edges on invalid_input for {param_name}={bad_value!r}, "
        f"got {data.get('edges')}"
    )
    assert data.get("error"), (
        f"Expected non-empty 'error' field for {param_name}={bad_value!r}"
    )
    return data


# ---------------------------------------------------------------------------
# Parameterized invalid cases — single consolidated test
# ---------------------------------------------------------------------------

INVALID_FILTER_CASES: list = [
    # (param_name, bad_value, test_id)
    ("source_domain", 123, "source_domain_int"),
    ("source_domain", {"alpha": 1, "bogus": 2}, "source_domain_dict"),
    ("source_domain", ("alpha", "beta"), "source_domain_tuple"),
    ("source_domain", {"alpha"}, "source_domain_set"),
    ("source_domain", True, "source_domain_bool"),
    ("source_domain", ["alpha", 123], "source_domain_list_with_non_str"),
    ("target_domain", 123, "target_domain_int"),
    ("target_domain", {"alpha": 1, "bogus": 2}, "target_domain_dict"),
    ("target_domain", ("alpha", "beta"), "target_domain_tuple"),
    ("target_domain", {"alpha"}, "target_domain_set"),
    ("target_domain", True, "target_domain_bool"),
    ("target_domain", ["beta", 42], "target_domain_list_with_non_str"),
    ("min_count", 0, "min_count_zero"),
    ("min_count", -1, "min_count_negative"),
    ("min_count", "3", "min_count_string"),
    ("min_count", 2.5, "min_count_float"),
    ("min_count", True, "min_count_bool_true"),
    ("min_count", False, "min_count_bool_false"),
]


@pytest.mark.parametrize(
    "param_name,bad_value",
    [(p, v) for p, v, _ in INVALID_FILTER_CASES],
    ids=[tid for _, _, tid in INVALID_FILTER_CASES],
)
def test_invalid_filter_param_returns_invalid_input(
    param_name: str, bad_value: Any, tmp_path: Path
) -> None:
    """All invalid filter param types/values must return the full invalid_input contract."""
    root = _make_minimal_graph(tmp_path)
    _assert_invalid_input(param_name, bad_value, root)


# ---------------------------------------------------------------------------
# Regression: valid inputs remain accepted after validation is added
# ---------------------------------------------------------------------------

VALID_FILTER_CASES: list = [
    # (param_name, good_value, test_id)
    ("source_domain", "alpha", "source_str"),
    ("source_domain", ["alpha", "beta"], "source_list_str"),
    ("source_domain", None, "source_none"),
    ("target_domain", "beta", "target_str"),
    ("target_domain", ["alpha", "beta"], "target_list_str"),
    ("target_domain", None, "target_none"),
    ("min_count", 1, "min_count_one"),
    ("min_count", 2, "min_count_two"),
    ("min_count", None, "min_count_none"),
]


@pytest.mark.parametrize(
    "param_name,good_value",
    [(p, v) for p, v, _ in VALID_FILTER_CASES],
    ids=[tid for _, _, tid in VALID_FILTER_CASES],
)
def test_valid_filter_param_returns_ok(
    param_name: str, good_value: Any, tmp_path: Path
) -> None:
    """Valid filter param values must still return success=true, resolution=ok."""
    root = _make_minimal_graph(tmp_path)
    data = _call_graph({param_name: good_value}, root)
    assert data["success"] is True, (
        f"Expected success=true for {param_name}={good_value!r}, "
        f"got success={data['success']}"
    )
    assert data["resolution"] == "ok", (
        f"Expected resolution='ok' for {param_name}={good_value!r}, "
        f"got resolution={data['resolution']!r}"
    )


def test_empty_params_returns_ok(tmp_path: Path) -> None:
    """No filter params returns full graph (backward-compat)."""
    root = _make_minimal_graph(tmp_path)
    data = _call_graph({}, root)
    assert data["success"] is True
    assert data["resolution"] == "ok"
