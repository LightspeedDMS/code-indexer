"""
Story #888 — AC1, AC2, AC6: Resolution field + empty-input BREAKING CHANGE + invariants.

AC1: every identifier-resolving depmap_* handler response includes a `resolution` field.
AC2: empty-string input returns success=false, resolution=invalid_input (BREAKING CHANGE
     from the previous success=true, [] behavior).
AC6: every response includes BOTH `success` and `resolution`; invariants hold:
     - resolution=invalid_input => success=false
     - resolution=ok => success=true
"""

import json
from pathlib import Path
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Shared helpers (mirroring test_depmap_handlers.py pattern)
# ---------------------------------------------------------------------------


def _make_user() -> User:
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


def _call_find_consumers(params: dict, app_state: MagicMock) -> Any:
    from code_indexer.server.mcp.handlers.depmap import depmap_find_consumers_handler

    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        app_state,
    ):
        return depmap_find_consumers_handler(params, _make_user())


def _call_get_repo_domains(params: dict, app_state: MagicMock) -> Any:
    from code_indexer.server.mcp.handlers.depmap import depmap_get_repo_domains_handler

    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        app_state,
    ):
        return depmap_get_repo_domains_handler(params, _make_user())


def _call_get_domain_summary(params: dict, app_state: MagicMock) -> Any:
    from code_indexer.server.mcp.handlers.depmap import (
        depmap_get_domain_summary_handler,
    )

    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        app_state,
    ):
        return depmap_get_domain_summary_handler(params, _make_user())


def _call_get_stale_domains(params: dict, app_state: MagicMock) -> Any:
    from code_indexer.server.mcp.handlers.depmap import (
        depmap_get_stale_domains_handler,
    )

    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        app_state,
    ):
        return depmap_get_stale_domains_handler(params, _make_user())


def _call_get_cross_domain_graph(params: dict, app_state: MagicMock) -> Any:
    from code_indexer.server.mcp.handlers.depmap import (
        depmap_get_cross_domain_graph_handler,
    )

    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        app_state,
    ):
        return depmap_get_cross_domain_graph_handler(params, _make_user())


def _make_valid_dep_map(tmp_path: Path) -> Path:
    """Create a minimal valid dep-map dir with empty domains. Returns tmp_path."""
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    (dep_map_dir / "_domains.json").write_text("[]", encoding="utf-8")
    return tmp_path


def _make_dep_map_with_consumer(tmp_path: Path, repo_name: str) -> Path:
    """Create a dep-map with one domain where repo_name is listed as a consumer.

    Incoming Dependencies table has a row where repo_name is in the 'Depends On'
    column, making find_consumers return a non-empty consumers list with resolution=ok.
    """
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    consumer_repo = "consumer-repo"
    domain = "alpha-domain"
    (dep_map_dir / "_domains.json").write_text(
        f'[{{"name":"{domain}","description":"d",'
        f'"participating_repos":["{repo_name}","{consumer_repo}"]}}]',
        encoding="utf-8",
    )
    content = (
        f"---\nname: {domain}\n---\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        f"| {consumer_repo} | {repo_name} | {domain} | Code-level | why | ev |\n"
    )
    (dep_map_dir / f"{domain}.md").write_text(content, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# AC1: resolution field present on every handler response
# ---------------------------------------------------------------------------


class TestAC1ResolutionFieldPresent:
    """AC1: resolution field is present in every depmap_* handler response."""

    def test_find_consumers_response_has_resolution(self, tmp_path: Path) -> None:
        """depmap_find_consumers response must include resolution field."""
        root = _make_valid_dep_map(tmp_path)
        result = _call_find_consumers({"repo_name": "some-repo"}, _make_app_state(root))
        data = _parse_response(result)
        assert "resolution" in data, (
            f"depmap_find_consumers response missing 'resolution' field; got keys: {list(data)}"
        )

    def test_get_repo_domains_response_has_resolution(self, tmp_path: Path) -> None:
        """depmap_get_repo_domains response must include resolution field."""
        root = _make_valid_dep_map(tmp_path)
        result = _call_get_repo_domains(
            {"repo_name": "some-repo"}, _make_app_state(root)
        )
        data = _parse_response(result)
        assert "resolution" in data, (
            f"depmap_get_repo_domains response missing 'resolution' field; got keys: {list(data)}"
        )

    def test_get_domain_summary_response_has_resolution(self, tmp_path: Path) -> None:
        """depmap_get_domain_summary response must include resolution field."""
        root = _make_valid_dep_map(tmp_path)
        result = _call_get_domain_summary(
            {"domain_name": "some-domain"}, _make_app_state(root)
        )
        data = _parse_response(result)
        assert "resolution" in data, (
            f"depmap_get_domain_summary response missing 'resolution' field; got keys: {list(data)}"
        )

    def test_get_stale_domains_response_has_resolution(self, tmp_path: Path) -> None:
        """depmap_get_stale_domains response must include resolution field."""
        root = _make_valid_dep_map(tmp_path)
        result = _call_get_stale_domains({"days_threshold": 0}, _make_app_state(root))
        data = _parse_response(result)
        assert "resolution" in data, (
            f"depmap_get_stale_domains response missing 'resolution' field; got keys: {list(data)}"
        )

    def test_get_cross_domain_graph_response_has_resolution(
        self, tmp_path: Path
    ) -> None:
        """depmap_get_cross_domain_graph response must include resolution field."""
        root = _make_valid_dep_map(tmp_path)
        result = _call_get_cross_domain_graph({}, _make_app_state(root))
        data = _parse_response(result)
        assert "resolution" in data, (
            f"depmap_get_cross_domain_graph response missing 'resolution' field; got keys: {list(data)}"
        )

    def test_resolution_field_is_string(self, tmp_path: Path) -> None:
        """resolution field must be a plain string (Literal, not Enum/int)."""
        root = _make_valid_dep_map(tmp_path)
        result = _call_find_consumers({"repo_name": "x"}, _make_app_state(root))
        data = _parse_response(result)
        assert isinstance(data.get("resolution"), str), (
            f"resolution must be a plain string, got: {type(data.get('resolution'))!r}"
        )

    def test_resolution_value_is_known_literal(self, tmp_path: Path) -> None:
        """resolution value must be one of the 5 known Literal strings."""
        known = {
            "ok",
            "invalid_input",
            "repo_not_indexed",
            "domain_not_indexed",
            "repo_has_no_consumers",
        }
        root = _make_valid_dep_map(tmp_path)
        result = _call_find_consumers({"repo_name": "x"}, _make_app_state(root))
        data = _parse_response(result)
        assert data.get("resolution") in known, (
            f"resolution value {data.get('resolution')!r} not in known literals: {known}"
        )


# ---------------------------------------------------------------------------
# AC2: empty-input BREAKING CHANGE
# ---------------------------------------------------------------------------


class TestAC2EmptyInputBreakingChange:
    """AC2: empty-string input must return success=false, resolution=invalid_input.

    BREAKING CHANGE: previous behavior was success=true with empty list.
    """

    def test_find_consumers_empty_repo_name_returns_invalid_input(
        self, tmp_path: Path
    ) -> None:
        """depmap_find_consumers with empty repo_name must return success=false/invalid_input."""
        root = _make_valid_dep_map(tmp_path)
        result = _call_find_consumers({"repo_name": ""}, _make_app_state(root))
        data = _parse_response(result)
        assert data["success"] is False, (
            "BREAKING CHANGE: empty repo_name must return success=false (was success=true)"
        )
        assert data.get("resolution") == "invalid_input", (
            f"Expected resolution=invalid_input, got: {data.get('resolution')!r}"
        )
        assert "error" in data, "Empty-input response must include an error field"

    def test_get_repo_domains_empty_repo_name_returns_invalid_input(
        self, tmp_path: Path
    ) -> None:
        """depmap_get_repo_domains with empty repo_name must return success=false/invalid_input."""
        root = _make_valid_dep_map(tmp_path)
        result = _call_get_repo_domains({"repo_name": ""}, _make_app_state(root))
        data = _parse_response(result)
        assert data["success"] is False
        assert data.get("resolution") == "invalid_input"
        assert "error" in data

    def test_get_domain_summary_empty_domain_name_returns_invalid_input(
        self, tmp_path: Path
    ) -> None:
        """depmap_get_domain_summary with empty domain_name must return success=false/invalid_input."""
        root = _make_valid_dep_map(tmp_path)
        result = _call_get_domain_summary({"domain_name": ""}, _make_app_state(root))
        data = _parse_response(result)
        assert data["success"] is False
        assert data.get("resolution") == "invalid_input"
        assert "error" in data

    def test_empty_string_not_silently_treated_as_success(self, tmp_path: Path) -> None:
        """Invariant: no depmap_* tool returns success=true with empty payload when input was empty.

        This is the core BREAKING CHANGE invariant from AC2.
        """
        root = _make_valid_dep_map(tmp_path)

        fc_result = _parse_response(
            _call_find_consumers({"repo_name": ""}, _make_app_state(root))
        )
        rd_result = _parse_response(
            _call_get_repo_domains({"repo_name": ""}, _make_app_state(root))
        )
        ds_result = _parse_response(
            _call_get_domain_summary({"domain_name": ""}, _make_app_state(root))
        )

        # Each must be success=false — never success=true with empty result
        for tool_name, data in [
            ("find_consumers", fc_result),
            ("get_repo_domains", rd_result),
            ("get_domain_summary", ds_result),
        ]:
            assert data["success"] is False, (
                f"{tool_name}: must return success=false for empty-string input "
                f"(BREAKING CHANGE from AC2)"
            )
            assert data.get("resolution") == "invalid_input", (
                f"{tool_name}: must return resolution=invalid_input for empty-string input"
            )


# ---------------------------------------------------------------------------
# AC6: success + resolution invariants
# ---------------------------------------------------------------------------


class TestAC6SuccessResolutionInvariants:
    """AC6: both success and resolution present; invariants hold."""

    def test_find_consumers_ok_resolution_implies_success_true(
        self, tmp_path: Path
    ) -> None:
        """When resolution=ok, success must be true.

        Uses a fixture that deterministically produces consumers — the handler
        must return resolution=ok and success=true together.
        """
        root = _make_dep_map_with_consumer(tmp_path, "target-repo")
        result = _call_find_consumers(
            {"repo_name": "target-repo"}, _make_app_state(root)
        )
        data = _parse_response(result)
        # Fixture guarantees a match — resolution must be ok
        assert data["resolution"] == "ok", (
            f"Expected resolution=ok for known-consumer fixture, got: {data.get('resolution')!r}"
        )
        assert data["success"] is True, (
            "Invariant violated: resolution=ok must imply success=true"
        )

    def test_invalid_input_resolution_implies_success_false(
        self, tmp_path: Path
    ) -> None:
        """When resolution=invalid_input, success must be false."""
        root = _make_valid_dep_map(tmp_path)
        result = _call_find_consumers({"repo_name": ""}, _make_app_state(root))
        data = _parse_response(result)
        assert data.get("resolution") == "invalid_input"
        assert data["success"] is False, (
            "Invariant violated: resolution=invalid_input must imply success=false"
        )

    def test_all_handlers_have_both_success_and_resolution(
        self, tmp_path: Path
    ) -> None:
        """Every handler response includes both success and resolution fields."""
        root = _make_valid_dep_map(tmp_path)
        responses = [
            (
                "find_consumers",
                _parse_response(
                    _call_find_consumers({"repo_name": "x"}, _make_app_state(root))
                ),
            ),
            (
                "get_repo_domains",
                _parse_response(
                    _call_get_repo_domains({"repo_name": "x"}, _make_app_state(root))
                ),
            ),
            (
                "get_domain_summary",
                _parse_response(
                    _call_get_domain_summary(
                        {"domain_name": "x"}, _make_app_state(root)
                    )
                ),
            ),
            (
                "get_stale_domains",
                _parse_response(
                    _call_get_stale_domains(
                        {"days_threshold": 0}, _make_app_state(root)
                    )
                ),
            ),
            (
                "get_cross_domain_graph",
                _parse_response(
                    _call_get_cross_domain_graph({}, _make_app_state(root))
                ),
            ),
        ]
        for name, data in responses:
            assert "success" in data, f"{name}: missing 'success' field"
            assert "resolution" in data, f"{name}: missing 'resolution' field"

    def test_missing_path_returns_success_false_with_resolution(
        self, tmp_path: Path
    ) -> None:
        """Missing dep_map_path response includes both success=false and resolution field.

        Missing dep_map_path is an infrastructure error — resolution is not invalid_input.
        """
        state = _make_app_state(tmp_path / "no-such-dir")
        result = _call_find_consumers({"repo_name": "x"}, state)
        data = _parse_response(result)
        assert data["success"] is False
        assert "resolution" in data
        # Missing path is not an invalid_input — it is a different resolution state
        assert data.get("resolution") != "invalid_input"


# ---------------------------------------------------------------------------
# AC1: ResolutionLiteral type contract exported from _depmap_aliases
# ---------------------------------------------------------------------------


class TestAC1ResolutionLiteral:
    """AC1: ResolutionLiteral Literal type is importable and has the correct 5 args."""

    def test_resolution_literal_importable(self) -> None:
        """ResolutionLiteral must be importable from _depmap_aliases."""
        from code_indexer.server.mcp.handlers._depmap_aliases import ResolutionLiteral

        assert ResolutionLiteral is not None

    def test_resolution_literal_has_five_values(self) -> None:
        """ResolutionLiteral must contain exactly the 5 valid resolution strings."""
        import typing
        from code_indexer.server.mcp.handlers._depmap_aliases import ResolutionLiteral

        # typing.get_args works on Literal types in Python 3.8+
        args = set(typing.get_args(ResolutionLiteral))
        expected = {
            "ok",
            "invalid_input",
            "repo_not_indexed",
            "domain_not_indexed",
            "repo_has_no_consumers",
        }
        assert args == expected, (
            f"ResolutionLiteral args mismatch. Expected {expected}, got {args}"
        )
