"""Bug #1423: pattern_name + list-typed repository_alias crash.

xray_search / xray_explore crashed with an unhandled
``TypeError: unsupported operand type(s) for /: 'PosixPath' and 'list'``
whenever ``pattern_name`` (stored pattern library) was combined with a
list-typed ``repository_alias`` -- even a single-element list.

Root cause: both handlers called ``_resolve_evaluator_code(params, repo_alias)``
(which resolves ``pattern_name`` via ``XrayPatternService._load_pattern`` --
``self._patterns_root / repo_alias / ...``) using the RAW, unnormalized
``repository_alias`` value, BEFORE the existing omni alias normalisation
(``_parse_json_string_array`` + single-element-list collapse) ran later in
the function. A list repo_alias therefore reached ``Path.__truediv__``
un-collapsed and crashed.

Fix: alias normalisation now runs before pattern resolution in both
handlers; a genuine multi-element list resolves patterns via the
cross-repo ``__any__`` scope (there is no single "owning" repo for an omni
multi-repo pattern lookup).

Scope table (from the bug report) covered here:
    Row 5: xray_search  + list (2 repos) + pattern_name  -- was CRASHING
    Row 6: xray_explore + list (2 repos) + pattern_name  -- was CRASHING
    Row 7: xray_search  + list (1 repo)  + pattern_name  -- was CRASHING
    Row 1: xray_search  + single string  + evaluator_code -- regression guard
    Row 2: xray_explore + single string  + evaluator_code -- regression guard
    Row 3: xray_search  + list (2 repos) + evaluator_code -- regression guard
    Row 4: xray_search  + single string  + pattern_name   -- regression guard

Mocking strategy (mirrors test_xray_pattern_handler.py / test_xray_multi_repo_bug1074.py):
- XrayPatternService: real (uses tmp_path filesystem), _git_commit mocked.
- Infra boundaries (_resolve_repo_path, _get_background_job_manager,
  _get_job_tracker, _get_xray_executor, validate_rust_evaluator,
  asyncio.get_running_loop): mocked.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Tuple, cast
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.xray_pattern_service import XrayPatternService

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_FAKE_REPO_PATH = "/fake/bug1423/repo/path"

DEEP_NESTING_PATTERN_YAML = """\
name: deep-nesting
description: "Finds control flow nested N+ levels deep"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }
"""


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _make_cidx_meta(tmp_path: Path) -> Path:
    cidx_meta = tmp_path / "data" / "golden-repos" / "cidx-meta"
    cidx_meta.mkdir(parents=True, exist_ok=True)
    return cidx_meta


def _store_deep_nesting_pattern(cidx_meta: Path) -> None:
    """Store the deep-nesting pattern in the cross-repo __any__ scope."""
    svc = XrayPatternService(cidx_meta)
    with patch.object(svc, "_git_commit"):
        svc.store_xray_pattern(
            scope="__any__",
            pattern_yaml=DEEP_NESTING_PATTERN_YAML,
        )


def _make_success_future() -> "asyncio.Future[Any]":
    f: asyncio.Future[Any] = asyncio.Future()
    f.set_result({"matches": [], "total_matches": 0})
    return f


@contextmanager
def _patched_xray_env(
    cidx_meta: Path,
    success_future_count: int = 1,
) -> Generator[Tuple[Any, Any, Any, Any], None, None]:
    """Patch infra boundaries shared by single-repo and multi-repo paths.

    Covers both handle_xray_search and handle_xray_explore, single-repo
    and multi-repo (omni) branches -- mirrors
    test_xray_multi_repo_bug1074._patched_xray_env_multi, with the
    addition of patching _get_cidx_meta_path so pattern_name resolution
    reads the real tmp_path pattern library.
    """
    mock_bjm = MagicMock()
    mock_jt = MagicMock()
    mock_jt.register_job.return_value = MagicMock()
    mock_xe = MagicMock()

    mock_app = MagicMock()
    mock_app.background_job_manager = mock_bjm
    mock_app.activated_repo_manager = None
    mock_app.golden_repo_manager = None

    futures: List["asyncio.Future[Any]"] = [
        _make_success_future() for _ in range(success_future_count)
    ]
    future_iter = iter(futures)

    with (
        patch("code_indexer.server.mcp.handlers._utils.app_module", mock_app),
        patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=_FAKE_REPO_PATH,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
            return_value=mock_bjm,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_job_tracker",
            return_value=mock_jt,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_xray_executor",
            return_value=mock_xe,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
            return_value=cidx_meta,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray.validate_rust_evaluator"
        ) as mock_validate,
        patch("asyncio.get_running_loop") as mock_loop,
    ):
        mock_validate.return_value = MagicMock(ok=True)
        mock_loop.return_value.run_in_executor.side_effect = lambda *a, **kw: next(
            future_iter
        )
        yield mock_bjm, mock_jt, mock_xe, mock_loop


# ---------------------------------------------------------------------------
# Rows 5 & 6 & 7 — crash reproduction (must NOT raise TypeError after fix)
# ---------------------------------------------------------------------------


class TestBug1423CrashReproduction:
    """Reproduce the exact crash shapes from the bug report's scope table."""

    @pytest.mark.asyncio
    async def test_row5_search_two_repo_list_pattern_name_no_typeerror(
        self, tmp_path: Path
    ) -> None:
        """Row 5: xray_search + list (2 repos) + pattern_name must not crash.

        Original bug: raised
        TypeError: unsupported operand type(s) for /: 'PosixPath' and 'list'
        """
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        cidx_meta = _make_cidx_meta(tmp_path)
        _store_deep_nesting_pattern(cidx_meta)
        user = _make_user()

        with _patched_xray_env(cidx_meta, success_future_count=2):
            try:
                result = await handle_xray_search(
                    {
                        "repository_alias": ["click-global", "typer-global"],
                        "pattern": "def ",
                        "search_target": "content",
                        "pattern_name": "deep-nesting",
                    },
                    user=user,
                )
            except TypeError as exc:
                pytest.fail(
                    f"Bug #1423 regression: handle_xray_search raised TypeError "
                    f"for list repository_alias + pattern_name: {exc}"
                )

        body = _parse_response(result)
        assert "job_ids" in body, f"Expected job_ids in response, got: {body}"
        assert len(body["job_ids"]) == 2
        assert body["errors"] == []

    @pytest.mark.asyncio
    async def test_row6_explore_two_repo_list_pattern_name_no_typeerror(
        self, tmp_path: Path
    ) -> None:
        """Row 6: xray_explore + list (2 repos) + pattern_name must not crash."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        cidx_meta = _make_cidx_meta(tmp_path)
        _store_deep_nesting_pattern(cidx_meta)
        user = _make_user()

        with _patched_xray_env(cidx_meta, success_future_count=2):
            try:
                result = await handle_xray_explore(
                    {
                        "repository_alias": ["click-global", "typer-global"],
                        "pattern": "def ",
                        "search_target": "content",
                        "pattern_name": "deep-nesting",
                    },
                    user=user,
                )
            except TypeError as exc:
                pytest.fail(
                    f"Bug #1423 regression: handle_xray_explore raised TypeError "
                    f"for list repository_alias + pattern_name: {exc}"
                )

        body = _parse_response(result)
        assert "job_ids" in body, f"Expected job_ids in response, got: {body}"
        assert len(body["job_ids"]) == 2
        assert body["errors"] == []

    @pytest.mark.asyncio
    async def test_row7_search_single_element_list_pattern_name_no_typeerror(
        self, tmp_path: Path
    ) -> None:
        """Row 7: xray_search + single-element list + pattern_name must not crash.

        Single-element list collapses to the single-repo response shape
        ({"job_id": ...}), per the v10.4.5 Defect 5 ergonomic behavior.
        """
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        cidx_meta = _make_cidx_meta(tmp_path)
        _store_deep_nesting_pattern(cidx_meta)
        user = _make_user()

        with _patched_xray_env(cidx_meta, success_future_count=1):
            try:
                result = await handle_xray_search(
                    {
                        "repository_alias": ["click-global"],
                        "pattern": "def ",
                        "search_target": "content",
                        "pattern_name": "deep-nesting",
                    },
                    user=user,
                )
            except TypeError as exc:
                pytest.fail(
                    f"Bug #1423 regression: handle_xray_search raised TypeError "
                    f"for single-element list repository_alias + pattern_name: {exc}"
                )

        body = _parse_response(result)
        assert "job_id" in body, f"Expected single-repo job_id shape, got: {body}"
        assert "job_ids" not in body


# ---------------------------------------------------------------------------
# Rows 1-4 — regression guards for already-working combinations
# ---------------------------------------------------------------------------


class TestBug1423RegressionAlreadyWorkingCombinations:
    """Prove rows 1-4 (already-working before this fix) remain unaffected."""

    @pytest.mark.asyncio
    async def test_row1_search_single_string_inline_evaluator_code(
        self, tmp_path: Path
    ) -> None:
        """Row 1: xray_search + single string alias + inline evaluator_code."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        cidx_meta = _make_cidx_meta(tmp_path)
        user = _make_user()

        with _patched_xray_env(cidx_meta, success_future_count=1):
            result = await handle_xray_search(
                {
                    "repository_alias": "myrepo-global",
                    "pattern": "def ",
                    "search_target": "content",
                    "evaluator_code": (
                        "fn evaluate_node(node: &OwnedNode) -> "
                        "Vec<EvalFinding> { vec![] }"
                    ),
                },
                user=user,
            )

        body = _parse_response(result)
        assert "job_id" in body
        assert "job_ids" not in body

    @pytest.mark.asyncio
    async def test_row2_explore_single_string_inline_evaluator_code(
        self, tmp_path: Path
    ) -> None:
        """Row 2: xray_explore + single string alias + inline evaluator_code."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        cidx_meta = _make_cidx_meta(tmp_path)
        user = _make_user()

        with _patched_xray_env(cidx_meta, success_future_count=1):
            result = await handle_xray_explore(
                {
                    "repository_alias": "myrepo-global",
                    "pattern": "def ",
                    "search_target": "content",
                    "evaluator_code": (
                        "fn evaluate_node(node: &OwnedNode) -> "
                        "Vec<EvalFinding> { vec![] }"
                    ),
                },
                user=user,
            )

        body = _parse_response(result)
        assert "job_id" in body
        assert "job_ids" not in body

    @pytest.mark.asyncio
    async def test_row3_search_two_repo_list_inline_evaluator_code(
        self, tmp_path: Path
    ) -> None:
        """Row 3: xray_search + list (2 repos) + inline evaluator_code."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        cidx_meta = _make_cidx_meta(tmp_path)
        user = _make_user()

        with _patched_xray_env(cidx_meta, success_future_count=2):
            result = await handle_xray_search(
                {
                    "repository_alias": ["click-global", "typer-global"],
                    "pattern": "def ",
                    "search_target": "content",
                    "evaluator_code": (
                        "fn evaluate_node(node: &OwnedNode) -> "
                        "Vec<EvalFinding> { vec![] }"
                    ),
                },
                user=user,
            )

        body = _parse_response(result)
        assert "job_ids" in body
        assert len(body["job_ids"]) == 2
        assert body["errors"] == []

    @pytest.mark.asyncio
    async def test_row4_search_single_string_pattern_name(self, tmp_path: Path) -> None:
        """Row 4: xray_search + single string alias + pattern_name."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        cidx_meta = _make_cidx_meta(tmp_path)
        _store_deep_nesting_pattern(cidx_meta)
        user = _make_user()

        with _patched_xray_env(cidx_meta, success_future_count=1):
            result = await handle_xray_search(
                {
                    "repository_alias": "myrepo-global",
                    "pattern": "def ",
                    "search_target": "content",
                    "pattern_name": "deep-nesting",
                },
                user=user,
            )

        body = _parse_response(result)
        assert "job_id" in body
        assert "job_ids" not in body
