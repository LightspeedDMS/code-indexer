"""X-Ray filename-target search: out-of-root symlink containment applies to
server context only.

Server-side X-Ray (MCP/REST handlers) constructs ``XRaySearchEngine()`` with
its default, which confines candidates and line-content reads to the
repository root. The local ``cidx xray search`` command searches the user's
own checkout and follows symlinks wherever they point, as it always did.

Drives the real CLI command and the real engine with the real xray-cli
binary over real temporary directories and real symlinks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

import pytest
from click.testing import CliRunner

pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

_ALWAYS_MATCH_EVALUATOR = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    '    vec![EvalFinding { pattern: "any".to_string(), line: node.start_line,'
    " snippet: String::new() }]\n"
    "}\n"
)


@pytest.fixture(autouse=True)
def _isolated_data_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path / "cidx-data"))
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server-data"))


def _repo_with_out_of_root_symlink(tmp_path: Path) -> Tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "legit.py").write_text("legit_marker_value = 1\n")
    outside = tmp_path / "shared-lib"
    outside.mkdir()
    (outside / "helper.py").write_text("outside_marker_value = 2\n")
    link = repo / "helper_link.py"
    link.symlink_to(outside / "helper.py")
    return repo, link


def test_local_cli_xray_search_follows_out_of_root_symlink(tmp_path: Path) -> None:
    from tests.unit.xray.conftest import require_xray_cli_binary

    require_xray_cli_binary()
    repo, _ = _repo_with_out_of_root_symlink(tmp_path)
    from code_indexer.cli import cli

    result = CliRunner().invoke(
        cli,
        [
            "xray",
            "search",
            "--repo",
            str(repo),
            "--regex",
            ".*",
            "--eval",
            _ALWAYS_MATCH_EVALUATOR,
            "--target",
            "filename",
            "--json",
            "--quiet",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    matched_files = {m["file_path"] for m in payload["matches"]}
    matched_content = " ".join(m.get("line_content", "") for m in payload["matches"])
    assert "helper_link.py" in matched_files, payload["matches"]
    assert "outside_marker_value" in matched_content, payload["matches"]
    assert "legit.py" in matched_files


def test_server_default_engine_excludes_out_of_root_symlink(tmp_path: Path) -> None:
    from tests.unit.xray.conftest import require_xray_cli_binary
    from code_indexer.xray.search_engine import XRaySearchEngine

    require_xray_cli_binary()
    repo, _ = _repo_with_out_of_root_symlink(tmp_path)

    result = XRaySearchEngine().run(
        repo_path=repo,
        driver_regex=r".*",
        evaluator_code=_ALWAYS_MATCH_EVALUATOR,
        search_target="filename",
    )

    matched_files = {m["file_path"] for m in result["matches"]}
    matched_content = " ".join(m.get("line_content", "") for m in result["matches"])
    assert "helper_link.py" not in matched_files
    assert "outside_marker_value" not in matched_content
    assert "legit.py" in matched_files
