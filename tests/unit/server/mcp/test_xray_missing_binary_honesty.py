"""A missing xray-cli binary must never look like a clean, empty scan.

THE BUG THIS PINS. ``RustNativeBackend.run_batch`` used to answer a missing
binary with one error tuple per file spec, each carrying that file's REAL
path. ``XRaySearchEngine.run`` discriminates a whole-batch failure by
``file_path == ""``, so those real paths made every unscanned file count
toward ``files_processed``. The job then reported
``files_processed == files_total``, ``matches: []``, no ``partial`` and no
``timeout`` -- byte-for-byte what a healthy scan over a repo with no hits
returns. An agent asking "does this repo still call prepareStatement?" was
told "no" by a server that had not opened a single file.

WHY THESE TESTS LIVE AT THE HANDLER LEVEL. A ``run_batch`` unit test can only
prove the tuple shape; it cannot prove the shape survives
``XRaySearchEngine.run``'s per-file accounting and the handler's truncation
step to reach the caller. These drive the real engine over a real temp repo
with the binary genuinely absent (via a CIDX_XRAY_CLI_PATH override that
points nowhere) and assert on the dict an MCP client would actually receive.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, cast
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.xray.rust_backend import _XRAY_CLI_PATH_ENV_VAR

VALID_EVAL = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"
DRIVER_REGEX = r"prepareStatement"


@pytest.fixture(autouse=True)
def _require_tree_sitter() -> None:
    """These tests run the real Phase 1 / Phase 2 engine."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")


@pytest.fixture
def repo_with_candidates(tmp_path: Path) -> Path:
    """A repo whose files all match DRIVER_REGEX, so Phase 1 yields candidates.

    files_total > 0 is what makes the bug observable at all: the old contract
    claimed every one of these had been evaluated.
    """
    for name in ("alpha.py", "beta.py", "gamma.py"):
        (tmp_path / name).write_text("conn.prepareStatement('select 1')\n")
    return tmp_path


@pytest.fixture
def no_xray_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force the backend to resolve to a binary that definitively is not there.

    Pointing the override at a missing path (rather than relying on the
    source-tree build output being absent) makes the test deterministic on
    both a developer machine that HAS built xray-cli and CI that has not.
    """
    missing = tmp_path / "absent" / "xray-cli"
    monkeypatch.setenv(_XRAY_CLI_PATH_ENV_VAR, str(missing))
    assert not missing.exists()
    return missing


def _make_user() -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap the MCP content envelope."""
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


@contextmanager
def _single_repo_env(module: str, repo_path: Path) -> Generator:
    """Patch the infra seams of one xray handler module (_search / _explore).

    Mirrors the established harness in test_xray_search_handler.py: everything
    outside the handler is mocked, the engine itself is real, and the job
    function is captured from run_in_executor rather than actually scheduled.
    """
    mock_app = MagicMock()
    mock_app.background_job_manager = MagicMock()
    mock_app.app.state.payload_cache = None
    mock_app.activated_repo_manager = None
    mock_app.golden_repo_manager = None

    loop_instance = MagicMock()
    loop_instance.run_in_executor.return_value = asyncio.Future()  # left pending

    base = f"code_indexer.server.mcp.handlers.xray.{module}"
    with (
        patch("code_indexer.server.mcp.handlers._utils.app_module", mock_app),
        patch(f"{base}._resolve_repo_path", return_value=str(repo_path)),
        patch(f"{base}._get_background_job_manager", return_value=MagicMock()),
        patch(f"{base}._get_job_tracker", return_value=MagicMock()),
        patch(f"{base}._get_xray_executor", return_value=MagicMock()),
        patch(f"{base}._get_xray_cell_limiter", return_value=None),
        patch(f"{base}.validate_rust_evaluator") as mock_validate,
        patch("asyncio.get_running_loop", return_value=loop_instance),
    ):
        mock_validate.return_value = MagicMock(ok=True)
        yield loop_instance


def _batch_level_binary_errors(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        e
        for e in result.get("evaluation_errors", [])
        if e.get("error_type") == "BinaryNotFound"
    ]


def _assert_not_a_clean_empty_scan(result: Dict[str, Any]) -> None:
    """The exact shape the old contract produced, asserted against."""
    files_total = result["files_total"]
    assert files_total > 0, (
        "fixture problem: Phase 1 found no candidates, so this test could not "
        "have detected the bug either way"
    )
    assert result["matches"] == []

    errors = _batch_level_binary_errors(result)
    assert errors, (
        "a scan run with no xray-cli binary must report BinaryNotFound to the "
        f"caller; got evaluation_errors={result.get('evaluation_errors')!r}"
    )
    assert all(e["file_path"] == "" for e in errors), (
        "BinaryNotFound must be batch-level (file_path == ''); a real file "
        "path makes XRaySearchEngine count an unscanned file as processed"
    )
    assert result["files_processed"] == 0, (
        "no file was opened, parsed or evaluated, so files_processed must be "
        f"0, not {result['files_processed']}"
    )
    assert result["files_processed"] != files_total, (
        "files_processed == files_total is the signature of a COMPLETE scan; "
        "a scan that never ran must not claim it"
    )


# ---------------------------------------------------------------------------
# xray_search
# ---------------------------------------------------------------------------


class TestXraySearchMissingBinary:
    async def test_search_job_result_reports_binary_not_found(
        self, repo_with_candidates: Path, no_xray_cli: Path
    ) -> None:
        """handle_xray_search's job result surfaces the missing binary."""
        from code_indexer.server.mcp.handlers.xray._search import handle_xray_search

        params = {
            "repository_alias": "myrepo-global",
            "pattern": DRIVER_REGEX,
            "evaluator_code": VALID_EVAL,
            "search_target": "content",
        }

        with _single_repo_env("_search", repo_with_candidates) as mock_loop:
            await handle_xray_search(params, _make_user())
            job_fn = mock_loop.run_in_executor.call_args[0][1]
            result = job_fn()

        _assert_not_a_clean_empty_scan(result)

    async def test_search_inline_response_carries_the_error(
        self, repo_with_candidates: Path, no_xray_cli: Path
    ) -> None:
        """The condition survives the MCP envelope, not just the job dict.

        await_seconds>0 returns the job result inline, which is the shape an
        agent reads directly -- including the handler's truncation pass, which
        must not drop a batch-level error on its way out.
        """
        from code_indexer.server.mcp.handlers.xray._search import handle_xray_search

        params = {
            "repository_alias": "myrepo-global",
            "pattern": DRIVER_REGEX,
            "evaluator_code": VALID_EVAL,
            "search_target": "content",
            "await_seconds": 5,
        }

        with _single_repo_env("_search", repo_with_candidates) as mock_loop:
            # Run the captured job synchronously and hand its result back as a
            # completed future, so the handler takes its inline-return path.
            captured: Dict[str, Any] = {}

            def _run_now(_executor, fn, *args):  # type: ignore[no-untyped-def]
                fut: asyncio.Future = asyncio.Future()
                fut.set_result(fn(*args))
                captured["result"] = fut.result()
                return fut

            mock_loop.run_in_executor.side_effect = _run_now
            response = await handle_xray_search(params, _make_user())

        body = _parse_response(response)
        assert "job_id" not in body, "await_seconds=5 must return inline"
        _assert_not_a_clean_empty_scan(body)


# ---------------------------------------------------------------------------
# xray_explore
# ---------------------------------------------------------------------------


class TestXrayExploreMissingBinary:
    async def test_explore_job_result_reports_binary_not_found(
        self, repo_with_candidates: Path, no_xray_cli: Path
    ) -> None:
        """xray_explore cannot return matches without the binary, and says so.

        xray_explore's AST-debug enrichment is pure Python tree-sitter, but it
        only ever decorates matches that Phase 2 produced -- with no binary
        there are none, so explore is just as blind as search and must report
        it rather than return an empty match list.
        """
        from code_indexer.server.mcp.handlers.xray._explore import handle_xray_explore

        params = {
            "repository_alias": "myrepo-global",
            "pattern": DRIVER_REGEX,
            "evaluator_code": VALID_EVAL,
            "search_target": "content",
            "max_debug_nodes": 30,
        }

        with _single_repo_env("_explore", repo_with_candidates) as mock_loop:
            await handle_xray_explore(params, _make_user())
            worker = mock_loop.run_in_executor.call_args[0][1]
            result = worker()

        _assert_not_a_clean_empty_scan(result)


# ---------------------------------------------------------------------------
# xray_search_batch
# ---------------------------------------------------------------------------


class TestXraySearchBatchMissingBinary:
    def test_batch_cell_marks_partial_and_tags_the_error(
        self, repo_with_candidates: Path, no_xray_cli: Path, tmp_path: Path
    ) -> None:
        """A blind cell must not be aggregated as a successful empty cell."""
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-binary": MagicMock(cancelled=False)}

        with patch(
            "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
            return_value=(VALID_EVAL, None),
        ):
            result = _run_xray_batch_job(
                resolved_repos=[
                    {"alias": "alpha-global", "path": repo_with_candidates}
                ],
                scans=[
                    {
                        "driver_regex": DRIVER_REGEX,
                        "search_target": "content",
                        "case_sensitive": True,
                        "multiline": False,
                        "pcre2": False,
                        "pattern_name": None,
                    }
                ],
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=60,
                job_id="jid-binary",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        assert result["matches"] == []
        binary_errors = [
            e
            for e in result["evaluation_errors"]
            if e.get("error_type") == "BinaryNotFound"
        ]
        assert binary_errors, (
            "the batch result must carry the cell's BinaryNotFound; got "
            f"{result['evaluation_errors']!r}"
        )
        err = binary_errors[0]
        assert err["file_path"] == ""
        assert err["repository_alias"] == "alpha-global"
        assert err["scan_index"] == 0
        assert result["partial"] is True, (
            "a batch whose only cell scanned nothing is not a complete batch"
        )
