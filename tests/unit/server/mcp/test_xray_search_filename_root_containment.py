"""``xray_search`` MCP handler, ``search_target="filename"`` -- front-door
confirmation that a symlink resolving outside the repository root never
reaches the returned matches.

Drives the REAL ``handle_xray_search`` handler end to end: real parameter
validation, real ``validate_rust_evaluator`` pre-flight, and a REAL
``XRaySearchEngine().run()`` call against a real temporary repository with
a real symlink (executed synchronously via a patched
``loop.run_in_executor`` so the inline ``await_seconds`` path returns
genuine matches without a background thread). Only the job-scheduling
infrastructure boundaries (``_resolve_repo_path``, ``_get_job_tracker``,
``_get_background_job_manager``, ``_get_xray_executor``) are stubbed --
none of the search logic itself is mocked (CLAUDE.md Foundation #1).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture(autouse=True)
def _isolated_server_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect every ``~/.cidx-server`` consumer to a tmp_path subdirectory
    instead of the real server data directory.

    Three independent mechanisms all need covering:

    1. Some call sites read ``CIDX_SERVER_DATA_DIR``/``CIDX_DATA_DIR``
       (falling back to ``Path.home()/.cidx-server`` when unset) at CALL
       time -- an env var redirects these.
    2. Others call ``Path.home()`` directly with no env-var override at
       all -- patching ``Path.home`` itself redirects these. Deliberately
       NOT done via the ``HOME`` environment variable: this test's real
       search engine spawns the real xray-cli subprocess to compile the
       evaluator, and that subprocess needs the REAL ``HOME`` to find its
       Rust toolchain (``~/.cargo``/``~/.rustup``) -- overriding the
       ``HOME`` env var breaks that lookup and hangs
       ``proc.communicate()`` waiting on rustc. The xray-cli subprocess's
       own cache resolution (rust/xray-core/src/cache.rs) reads
       ``CIDX_DATA_DIR`` (inherited from this process's environment,
       unlike ``Path.home()`` patching, which is Python-only) before
       falling back to ``HOME`` -- setting ``CIDX_DATA_DIR`` redirects it
       without touching ``HOME``.
    3. Several ``deployment_executor.py`` path constants
       (``LAUNCH_CONFIG_PATH``, ``APPLIED_LAUNCH_CONFIG_PATH``) are
       computed ONCE at import time from the real home, and other modules
       (``config_service``, ``auto_update.service``) each bind their OWN
       separate name to that same value via ``from ... import`` --
       patching ``Path.home`` after those modules already imported can
       never reach an already-frozen module-level constant, and patching
       only ``deployment_executor``'s own attribute does not change a
       DIFFERENT module's separate binding of the same name. Every such
       binding is patched explicitly below."""
    isolated_home = tmp_path / "isolated_home"
    isolated_home.mkdir(exist_ok=True)
    isolated_data_dir = isolated_home / ".cidx-server"
    isolated_data_dir.mkdir(exist_ok=True)

    monkeypatch.setattr(Path, "home", lambda: isolated_home)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(isolated_data_dir))
    # Inherited by the xray-cli subprocess (rust/xray-core/src/cache.rs
    # resolves its own cache from this same variable before falling back
    # to HOME).
    monkeypatch.setenv("CIDX_DATA_DIR", str(isolated_data_dir))

    isolated_launch_path = isolated_data_dir / "launch.json"
    isolated_applied_launch_path = isolated_data_dir / "applied_launch.json"

    from code_indexer.server.auto_update import deployment_executor
    from code_indexer.server.auto_update import service as auto_update_service
    from code_indexer.server.services import config_service

    for module in (deployment_executor, config_service):
        monkeypatch.setattr(module, "LAUNCH_CONFIG_PATH", isolated_launch_path)
        monkeypatch.setattr(
            module, "APPLIED_LAUNCH_CONFIG_PATH", isolated_applied_launch_path
        )
    monkeypatch.setattr(
        auto_update_service,
        "APPLIED_LAUNCH_CONFIG_PATH",
        isolated_applied_launch_path,
    )


_ALWAYS_MATCH_EVALUATOR = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    '    vec![EvalFinding { pattern: "any".to_string(), line: node.start_line,'
    " snippet: String::new() }]\n"
    "}\n"
)


def _make_user() -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    parsed: Dict[str, Any] = json.loads(result["content"][0]["text"])
    return parsed


@contextmanager
def _real_engine_env(repo_path: Path) -> Generator[None, None, None]:
    """Patch only the job-scheduling infrastructure boundaries;
    ``run_in_executor`` actually calls the job function synchronously so
    the REAL ``XRaySearchEngine`` executes against ``repo_path``."""
    mock_bjm = MagicMock()
    mock_jt = MagicMock()
    mock_jt.register_job.return_value = MagicMock()
    mock_exec = MagicMock()

    def _run_sync(executor: Any, fn: Any) -> "asyncio.Future[Any]":
        future: "asyncio.Future[Any]" = asyncio.Future()
        try:
            future.set_result(fn())
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    loop_instance = MagicMock()
    loop_instance.run_in_executor.side_effect = _run_sync

    from code_indexer.server import app as real_app_module

    with (
        patch.object(
            real_app_module.app.state,
            "access_filtering_service",
            MagicMock(is_admin_user=MagicMock(return_value=True)),
            create=True,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._search._resolve_repo_path",
            return_value=str(repo_path),
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._search._get_background_job_manager",
            return_value=mock_bjm,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._search._get_job_tracker",
            return_value=mock_jt,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._search._get_xray_executor",
            return_value=mock_exec,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._search._get_xray_cell_limiter",
            return_value=None,
        ),
        patch("asyncio.get_running_loop", return_value=loop_instance),
    ):
        yield


async def _run_filename_search(repo_path: Path) -> Dict[str, Any]:
    from code_indexer.server.mcp.protocol import handle_tools_call

    call_params = {
        "name": "xray_search",
        "arguments": {
            "repository_alias": "example-global",
            "pattern": r".*",
            "evaluator_code": _ALWAYS_MATCH_EVALUATOR,
            "search_target": "filename",
            "await_seconds": 10,
        },
    }
    with _real_engine_env(repo_path):
        result = await handle_tools_call(call_params, _make_user())
    return _parse_response(result)


class TestXraySearchFilenameFrontDoorRootContainment:
    @pytest.mark.asyncio
    async def test_outside_symlink_content_never_reaches_matches(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "legit.py").write_text("legit_marker_value = 1\n")

        outside_target = tmp_path / "outside_target.py"
        outside_target.write_text("outside_marker_value = 2\n")
        escape_link = repo / "escape_link.py"
        escape_link.symlink_to(outside_target)

        data = await _run_filename_search(repo)

        assert "matches" in data, f"expected inline matches, got: {data}"
        matched_files = {m["file_path"] for m in data["matches"]}
        matched_content = " ".join(m.get("line_content", "") for m in data["matches"])

        assert "escape_link.py" not in matched_files, (
            f"A symlink resolving outside the repository root produced a "
            f"match via the MCP front door. Matches: {data['matches']}"
        )
        assert "outside_marker_value" not in matched_content
        assert "legit.py" in matched_files
        assert "legit_marker_value" in matched_content

    @pytest.mark.asyncio
    async def test_inside_symlink_content_still_reaches_matches(
        self, tmp_path: Path
    ) -> None:
        """No regression: a symlink resolving inside the repository root
        still produces a match through the MCP front door."""
        repo = tmp_path / "repo"
        repo.mkdir()
        real_target = repo / "real.py"
        real_target.write_text("inside_marker_value = 3\n")
        inside_link = repo / "inside_link.py"
        inside_link.symlink_to(real_target)

        data = await _run_filename_search(repo)

        assert "matches" in data, f"expected inline matches, got: {data}"
        matched_files = {m["file_path"] for m in data["matches"]}
        matched_content = " ".join(m.get("line_content", "") for m in data["matches"])

        assert "inside_link.py" in matched_files, (
            f"A symlink resolving inside the repository root must still "
            f"produce a match through the MCP front door (no regression). "
            f"Matches: {data['matches']}"
        )
        assert "inside_marker_value" in matched_content
