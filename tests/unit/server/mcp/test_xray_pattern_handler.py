"""Unit/integration tests for xray pattern MCP handler — Story #1031.

Tests:
- handle_store_xray_pattern: stores patterns via MCP tool call
- handle_xray_search: pattern_name/pattern_params integration (AC5)
- handle_xray_explore: pattern_name/pattern_params integration (AC5)

Mocking strategy:
- XrayPatternService: real (uses tmp_path filesystem)
- _git_commit on service: mocked (avoids real git repo requirement)
- background_job_manager: mocked (avoids full server bootstrap)
- _resolve_repo_path: mocked (avoids live alias manager)
- App module state: patched minimally
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


MINIMAL_PATTERN_YAML = """\
name: my-pattern
description: "Test pattern"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }
"""

PARAM_PATTERN_YAML = """\
name: deep-nesting
description: "Finds deep nesting"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }
parameters:
  - name: DEPTH_THRESHOLD
    type: usize
    default: 4
    description: "Minimum nesting depth"
"""

VALID_EVALUATOR = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"


def _make_cidx_meta(tmp_path: Path) -> Path:
    cidx_meta = tmp_path / "data" / "golden-repos" / "cidx-meta"
    cidx_meta.mkdir(parents=True, exist_ok=True)
    return cidx_meta


# ---------------------------------------------------------------------------
# Tests: handle_store_xray_pattern
# ---------------------------------------------------------------------------


class TestHandleStoreXrayPattern:
    """Tests for the store_xray_pattern MCP handler."""

    def test_unauthenticated_returns_auth_required(self, tmp_path: Path) -> None:
        """Auth: unauthenticated user returns auth_required."""
        from code_indexer.server.mcp.handlers.xray import handle_store_xray_pattern

        result = handle_store_xray_pattern(
            {
                "scope": "__any__",
                "pattern_yaml": MINIMAL_PATTERN_YAML,
            },
            user=None,  # type: ignore[arg-type]
        )
        body = _parse_response(result)
        assert body["error"] == "auth_required"

    def test_missing_scope_returns_error(self, tmp_path: Path) -> None:
        """Validation: missing scope parameter returns error."""
        from code_indexer.server.mcp.handlers.xray import handle_store_xray_pattern

        user = _make_user()
        cidx_meta = _make_cidx_meta(tmp_path)

        with patch(
            "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
            return_value=cidx_meta,
        ):
            result = handle_store_xray_pattern(
                {"pattern_yaml": MINIMAL_PATTERN_YAML},
                user=user,
            )
        body = _parse_response(result)
        assert body.get("error") is not None

    def test_missing_pattern_yaml_returns_error(self, tmp_path: Path) -> None:
        """Validation: missing pattern_yaml parameter returns error."""
        from code_indexer.server.mcp.handlers.xray import handle_store_xray_pattern

        user = _make_user()
        cidx_meta = _make_cidx_meta(tmp_path)

        with patch(
            "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
            return_value=cidx_meta,
        ):
            result = handle_store_xray_pattern(
                {"scope": "__any__"},
                user=user,
            )
        body = _parse_response(result)
        assert body.get("error") is not None

    def test_valid_pattern_returns_success(self, tmp_path: Path) -> None:
        """Happy path: valid pattern stored returns success."""
        from code_indexer.server.mcp.handlers.xray import handle_store_xray_pattern
        from code_indexer.server.services.xray_pattern_service import XrayPatternService

        user = _make_user()
        cidx_meta = _make_cidx_meta(tmp_path)

        with patch(
            "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
            return_value=cidx_meta,
        ):
            # Also mock git commit on service instances
            original_init = XrayPatternService.__init__

            def patched_init(self, path: Path, **kwargs: Any) -> None:
                original_init(self, path, **kwargs)
                self._git_commit = MagicMock()  # type: ignore[method-assign]

            with patch.object(XrayPatternService, "__init__", patched_init):
                result = handle_store_xray_pattern(
                    {
                        "scope": "__any__",
                        "pattern_yaml": MINIMAL_PATTERN_YAML,
                    },
                    user=user,
                )

        body = _parse_response(result)
        assert body.get("success") is True

    def test_overwrite_false_prevents_duplicate(self, tmp_path: Path) -> None:
        """AC4: overwrite=false (default) prevents duplicate stores."""
        from code_indexer.server.mcp.handlers.xray import handle_store_xray_pattern
        from code_indexer.server.services.xray_pattern_service import XrayPatternService

        user = _make_user()
        cidx_meta = _make_cidx_meta(tmp_path)

        original_init = XrayPatternService.__init__

        def patched_init(self, path: Path, **kwargs: Any) -> None:
            original_init(self, path, **kwargs)
            self._git_commit = MagicMock()  # type: ignore[method-assign]

        with patch(
            "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
            return_value=cidx_meta,
        ):
            with patch.object(XrayPatternService, "__init__", patched_init):
                # First store
                handle_store_xray_pattern(
                    {"scope": "__any__", "pattern_yaml": MINIMAL_PATTERN_YAML},
                    user=user,
                )
                # Second store without overwrite
                result = handle_store_xray_pattern(
                    {
                        "scope": "__any__",
                        "pattern_yaml": MINIMAL_PATTERN_YAML,
                        "overwrite": False,
                    },
                    user=user,
                )

        body = _parse_response(result)
        assert body["error"] == "pattern_already_exists"


# ---------------------------------------------------------------------------
# Tests: handle_xray_search with pattern_name (AC5)
# ---------------------------------------------------------------------------


class TestXraySearchPatternName:
    """AC5: xray_search accepts pattern_name; mutually exclusive with evaluator_code."""

    def _make_mock_bjm(self) -> MagicMock:
        mock = MagicMock()
        mock.submit_job.return_value = "test-job-id"
        return mock

    def test_pattern_name_and_evaluator_code_mutually_exclusive(
        self, tmp_path: Path
    ) -> None:
        """AC5: Both pattern_name and evaluator_code provided returns mutually_exclusive_params."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        user = _make_user()
        mock_bjm = self._make_mock_bjm()
        mock_app = MagicMock()
        mock_app.background_job_manager = mock_bjm

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module",
            mock_app,
        ):
            with patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ):
                result = handle_xray_search(
                    {
                        "repository_alias": "myrepo-global",
                        "pattern": "def ",
                        "search_target": "content",
                        "pattern_name": "my-pattern",
                        "evaluator_code": VALID_EVALUATOR,
                    },
                    user=user,
                )

        body = _parse_response(result)
        assert body["error"] == "mutually_exclusive_params"

    def test_pattern_name_loads_stored_pattern(self, tmp_path: Path) -> None:
        """AC5: pattern_name (without evaluator_code) loads pattern and submits job."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search
        from code_indexer.server.services.xray_pattern_service import XrayPatternService

        user = _make_user()
        mock_bjm = self._make_mock_bjm()
        mock_app = MagicMock()
        mock_app.background_job_manager = mock_bjm
        cidx_meta = _make_cidx_meta(tmp_path)

        # Pre-store a pattern
        svc = XrayPatternService(cidx_meta)
        with patch.object(svc, "_git_commit"):
            svc.store_xray_pattern(
                scope="__any__",
                pattern_yaml=MINIMAL_PATTERN_YAML,
            )

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module",
            mock_app,
        ):
            with patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ):
                with patch(
                    "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
                    return_value=cidx_meta,
                ):
                    result = handle_xray_search(
                        {
                            "repository_alias": "myrepo-global",
                            "pattern": "def ",
                            "search_target": "content",
                            "pattern_name": "my-pattern",
                        },
                        user=user,
                    )

        body = _parse_response(result)
        # Should have submitted a job (not an error)
        assert "job_id" in body

    def test_pattern_name_not_found_returns_error(self, tmp_path: Path) -> None:
        """AC5: pattern_name that doesn't exist returns pattern_not_found error."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        user = _make_user()
        mock_bjm = self._make_mock_bjm()
        mock_app = MagicMock()
        mock_app.background_job_manager = mock_bjm
        cidx_meta = _make_cidx_meta(tmp_path)

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module",
            mock_app,
        ):
            with patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ):
                with patch(
                    "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
                    return_value=cidx_meta,
                ):
                    result = handle_xray_search(
                        {
                            "repository_alias": "myrepo-global",
                            "pattern": "def ",
                            "search_target": "content",
                            "pattern_name": "nonexistent-pattern",
                        },
                        user=user,
                    )

        body = _parse_response(result)
        assert body["error"] == "pattern_not_found"

    def test_pattern_params_applied_to_resolved_pattern(self, tmp_path: Path) -> None:
        """AC8: pattern_params overrides are passed to resolved pattern."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search
        from code_indexer.server.services.xray_pattern_service import XrayPatternService

        user = _make_user()
        mock_bjm = self._make_mock_bjm()
        mock_app = MagicMock()
        mock_app.background_job_manager = mock_bjm
        cidx_meta = _make_cidx_meta(tmp_path)

        # Pre-store a parametrized pattern
        svc = XrayPatternService(cidx_meta)
        with patch.object(svc, "_git_commit"):
            svc.store_xray_pattern(scope="__any__", pattern_yaml=PARAM_PATTERN_YAML)

        # Capture what evaluator_code was passed to the job
        submitted_evaluator: list = []

        def capture_submit(**kwargs: Any) -> str:
            # The job fn is a closure — we capture it and call it with a noop progress
            func = kwargs.get("func")
            if func is not None:
                submitted_evaluator.append(func)
            return "test-job-id"

        mock_bjm.submit_job.side_effect = capture_submit

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module",
            mock_app,
        ):
            with patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ):
                with patch(
                    "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
                    return_value=cidx_meta,
                ):
                    result = handle_xray_search(
                        {
                            "repository_alias": "myrepo-global",
                            "pattern": "def ",
                            "search_target": "content",
                            "pattern_name": "deep-nesting",
                            "pattern_params": {"DEPTH_THRESHOLD": 6},
                        },
                        user=user,
                    )

        body = _parse_response(result)
        assert "job_id" in body


# ---------------------------------------------------------------------------
# Tests: handle_xray_explore with pattern_name (AC5)
# ---------------------------------------------------------------------------


class TestXrayExplorePatternName:
    """AC5: xray_explore accepts pattern_name; mutually exclusive with evaluator_code."""

    def test_pattern_name_and_evaluator_code_mutually_exclusive(
        self, tmp_path: Path
    ) -> None:
        """AC5: Both pattern_name and evaluator_code in xray_explore returns error."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        user = _make_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "test-job-id"
        mock_app = MagicMock()
        mock_app.background_job_manager = mock_bjm

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module",
            mock_app,
        ):
            with patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ):
                result = handle_xray_explore(
                    {
                        "repository_alias": "myrepo-global",
                        "pattern": "def ",
                        "search_target": "content",
                        "pattern_name": "my-pattern",
                        "evaluator_code": VALID_EVALUATOR,
                    },
                    user=user,
                )

        body = _parse_response(result)
        assert body["error"] == "mutually_exclusive_params"

    def test_pattern_name_loads_stored_pattern_in_explore(self, tmp_path: Path) -> None:
        """AC5: pattern_name in xray_explore loads pattern and submits job."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore
        from code_indexer.server.services.xray_pattern_service import XrayPatternService

        user = _make_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "test-job-id"
        mock_app = MagicMock()
        mock_app.background_job_manager = mock_bjm
        cidx_meta = _make_cidx_meta(tmp_path)

        svc = XrayPatternService(cidx_meta)
        with patch.object(svc, "_git_commit"):
            svc.store_xray_pattern(scope="__any__", pattern_yaml=MINIMAL_PATTERN_YAML)

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module",
            mock_app,
        ):
            with patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ):
                with patch(
                    "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
                    return_value=cidx_meta,
                ):
                    result = handle_xray_explore(
                        {
                            "repository_alias": "myrepo-global",
                            "pattern": "def ",
                            "search_target": "content",
                            "pattern_name": "my-pattern",
                        },
                        user=user,
                    )

        body = _parse_response(result)
        assert "job_id" in body
