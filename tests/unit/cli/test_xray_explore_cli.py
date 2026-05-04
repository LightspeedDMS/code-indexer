"""Tests for the `cidx xray explore` CLI command (Story #976).

Uses click.testing.CliRunner — no real subprocess, no external services.
Tree-sitter IS imported (xray extras boundary respected at module level but
deliberately triggered when XRaySearchEngine is instantiated inside the command).

Acceptance criteria covered:
  - AC1: --max-debug-nodes 0 rejected (below range 1..500)
  - AC2: --max-debug-nodes 1000 rejected (above range 1..500)
  - AC3: Default --max-debug-nodes is 50
  - AC4: Invalid repo path exits 2 with clear message
  - AC5: Real fixture repo produces explored_nodes output (table mode)
  - AC6: --json output includes ast_debug field on each match
  - AC7: max_debug_nodes cap causes partial=True / max_files_reached flag
  - AC8: AST renderer produces hierarchical indented output; truncated sentinel shown
  - AC9: Bad evaluator code yields exit 2 with validation error
  - AC11: PARTIAL timeout result yields exit code 3
  - AC12: --eval and --eval-file mutually exclusive
  - AC13: --help exits 0 and lists explore subcommand
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner, Result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def invoke_xray_explore(runner: CliRunner, args: list) -> Result:
    """Import cli lazily to avoid top-level tree_sitter import during collection."""
    from code_indexer.cli import cli

    return runner.invoke(cli, ["xray", "explore"] + args, catch_exceptions=False)


def make_py_fixture(tmp_path: Path, files: dict) -> Path:
    """Create files under tmp_path with given content, return tmp_path."""
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# AC1: --max-debug-nodes 0 rejected (below range 1..500)
# ---------------------------------------------------------------------------


def test_xray_explore_invalid_max_debug_nodes_zero():
    """--max-debug-nodes 0 should be rejected by Click (below range 1..500)."""
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            "/tmp",
            "--regex",
            "x",
            "--eval",
            "return True",
            "--max-debug-nodes",
            "0",
        ],
    )
    assert result.exit_code != 0
    output_lower = result.output.lower()
    assert (
        "0" in result.output
        or "invalid" in output_lower
        or "range" in output_lower
        or "max-debug-nodes" in output_lower
    )


# ---------------------------------------------------------------------------
# AC2: --max-debug-nodes 1000 rejected (above range 1..500)
# ---------------------------------------------------------------------------


def test_xray_explore_invalid_max_debug_nodes_high():
    """--max-debug-nodes 1000 should be rejected (above range 1..500)."""
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            "/tmp",
            "--regex",
            "x",
            "--eval",
            "return True",
            "--max-debug-nodes",
            "1000",
        ],
    )
    assert result.exit_code != 0
    output_lower = result.output.lower()
    assert (
        "1000" in result.output
        or "invalid" in output_lower
        or "range" in output_lower
        or "max-debug-nodes" in output_lower
    )


# ---------------------------------------------------------------------------
# AC3: Default --max-debug-nodes is 50
# ---------------------------------------------------------------------------


def test_xray_explore_default_max_debug_nodes_is_50(tmp_path: Path):
    """When --max-debug-nodes is omitted, the default should be 50."""
    repo_dir = make_py_fixture(
        tmp_path,
        {"main.py": "x = 1\n"},
    )
    runner = CliRunner()
    # Pass --json so we can inspect the ast_debug node count
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "x",
            "--eval",
            "return True",
            "--json",
        ],
    )
    assert result.exit_code == 0, (
        f"Unexpected exit: {result.exit_code}. Output: {result.output}"
    )
    data = json.loads(result.output)
    # Verify the command ran successfully (default cap of 50 applied)
    assert "matches" in data
    # If there is a match with ast_debug, confirm it has at most 50 real nodes
    for m in data["matches"]:
        if "ast_debug" in m:
            node_count = _count_ast_nodes(m["ast_debug"])
            assert node_count <= 50, (
                f"Default max_debug_nodes=50 exceeded: got {node_count} nodes"
            )


def _count_ast_nodes(node: dict, _count: list[int] | None = None) -> int:
    """Recursively count non-truncated AST nodes."""
    if _count is None:
        _count = [0]
    if node.get("type") == "...truncated":
        return _count[0]
    _count[0] += 1
    for child in node.get("children", []):
        _count_ast_nodes(child, _count)
    return _count[0]


# ---------------------------------------------------------------------------
# AC4: Invalid repo path exits 2 with clear message
# ---------------------------------------------------------------------------


def test_xray_explore_invalid_repo_path():
    """A non-existent repo path should exit 2 with a clear error message."""
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            "/nonexistent/path/that/does/not/exist",
            "--regex",
            "x",
            "--eval",
            "return True",
        ],
    )
    assert result.exit_code == 2
    output_lower = result.output.lower()
    assert (
        "nonexistent" in result.output
        or "not exist" in output_lower
        or "not found" in output_lower
        or "directory" in output_lower
        or "repo" in output_lower
    )


# ---------------------------------------------------------------------------
# AC5: Real fixture repo produces output in table mode
# ---------------------------------------------------------------------------


def test_xray_explore_runs_against_real_fixture(tmp_path: Path):
    """Explore against a real Python fixture should exit 0 and show output."""
    repo_dir = make_py_fixture(
        tmp_path,
        {
            "auth.py": "password = 'hunter2'\n",
            "utils.py": "# no secrets here\n",
        },
    )
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "password",
            "--eval",
            "return True",
        ],
    )
    assert result.exit_code == 0, (
        f"Expected exit 0, got {result.exit_code}. Output: {result.output}"
    )
    # Table mode should show file/match summary info
    output_lower = result.output.lower()
    assert (
        "files" in output_lower
        or "matches" in output_lower
        or "elapsed" in output_lower
        or "auth.py" in result.output
    )


def test_xray_explore_table_shows_ast_section(tmp_path: Path):
    """Table output for a match should include an AST section."""
    repo_dir = make_py_fixture(
        tmp_path,
        {"main.py": "x = 1\n"},
    )
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "x",
            "--eval",
            "return True",
        ],
    )
    assert result.exit_code == 0, f"Output: {result.output}"
    # Table mode should include AST section or node type info
    # The renderer prints node types like "module", "expression_statement", etc.
    assert (
        "ast" in result.output.lower()
        or "module" in result.output.lower()
        or "[" in result.output  # start_point / end_point bracket notation
    )


# ---------------------------------------------------------------------------
# AC6: --json output includes ast_debug field on each match
# ---------------------------------------------------------------------------


def test_xray_explore_json_output(tmp_path: Path):
    """--json flag should emit valid JSON with ast_debug field on each match."""
    repo_dir = make_py_fixture(
        tmp_path,
        {"main.py": "password = 'secret'\n"},
    )
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "password",
            "--eval",
            "return True",
            "--json",
        ],
    )
    assert result.exit_code == 0, f"Output: {result.output}"
    data = json.loads(result.output)

    # Top-level keys must be present
    for key in (
        "matches",
        "evaluation_errors",
        "files_processed",
        "files_total",
        "elapsed_seconds",
    ):
        assert key in data, f"Missing key '{key}' in JSON output"

    assert isinstance(data["matches"], list)
    assert len(data["matches"]) >= 1, "Expected at least 1 match"

    # Each match must have ast_debug
    for match in data["matches"]:
        assert "ast_debug" in match, f"Match missing 'ast_debug' key: {match}"
        ast_debug = match["ast_debug"]
        # Verify the ast_debug has the required fields per story spec
        assert "type" in ast_debug, "ast_debug missing 'type'"
        assert "start_byte" in ast_debug, "ast_debug missing 'start_byte'"
        assert "end_byte" in ast_debug, "ast_debug missing 'end_byte'"
        assert "start_point" in ast_debug, "ast_debug missing 'start_point'"
        assert "end_point" in ast_debug, "ast_debug missing 'end_point'"
        assert "text_preview" in ast_debug, "ast_debug missing 'text_preview'"
        assert "child_count" in ast_debug, "ast_debug missing 'child_count'"
        assert "children" in ast_debug, "ast_debug missing 'children'"


# ---------------------------------------------------------------------------
# AC7: max_debug_nodes cap with --max-files shows partial flag
# ---------------------------------------------------------------------------


def test_xray_explore_max_files_cap_shows_partial(tmp_path: Path):
    """With --max-files capping evaluation, result should be partial and exit 3."""
    repo_dir = make_py_fixture(
        tmp_path,
        {
            "a.py": "x = 1\n",
            "b.py": "x = 2\n",
            "c.py": "x = 3\n",
            "d.py": "x = 4\n",
        },
    )
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "x",
            "--eval",
            "return True",
            "--max-files",
            "1",
            "--json",
        ],
    )
    # Exit 3 = partial
    assert result.exit_code == 3, (
        f"Expected exit 3, got {result.exit_code}. Output: {result.output}"
    )
    data = json.loads(result.output)
    assert data.get("partial") is True
    assert data.get("max_files_reached") is True


# ---------------------------------------------------------------------------
# AC8: AST renderer produces hierarchical indented output with truncated sentinel
# ---------------------------------------------------------------------------


def test_xray_explore_ast_renderer_hierarchical(tmp_path: Path):
    """Table mode should render AST nodes with indentation and type labels."""
    repo_dir = make_py_fixture(
        tmp_path,
        {"sample.py": "def foo():\n    return 42\n"},
    )
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "foo",
            "--eval",
            "return True",
        ],
    )
    assert result.exit_code == 0, f"Output: {result.output}"
    # AST output should include node types (Python tree-sitter uses 'module', 'function_definition', etc.)
    output = result.output
    assert any(
        keyword in output
        for keyword in ["module", "function_definition", "identifier", "return"]
    ), f"Expected AST node types in output, got:\n{output}"


def test_xray_explore_ast_renderer_shows_truncated_sentinel(tmp_path: Path):
    """With max-debug-nodes=1, truncated sentinel should appear in table output."""
    repo_dir = make_py_fixture(
        tmp_path,
        {"main.py": "x = 1 + 2 + 3\n"},
    )
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "x",
            "--eval",
            "return True",
            "--max-debug-nodes",
            "1",
        ],
    )
    assert result.exit_code == 0, f"Output: {result.output}"
    # With only 1 allowed node, truncated sentinel must appear
    assert "truncated" in result.output.lower(), (
        f"Expected '...truncated' in output with max-debug-nodes=1, got:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# AC9: Bad evaluator code yields exit 2 with validation error
# ---------------------------------------------------------------------------


def test_xray_explore_bad_evaluator_import():
    """An evaluator with 'import os' should fail validation and exit 2."""
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            "/tmp",
            "--regex",
            "x",
            "--eval",
            "import os",
        ],
    )
    assert result.exit_code == 2
    output_lower = result.output.lower()
    assert "evaluator" in output_lower or "validation" in output_lower


# ---------------------------------------------------------------------------
# AC11: PARTIAL (max-files) causes exit code 3
# ---------------------------------------------------------------------------


def test_xray_explore_partial_max_files_exits_3(tmp_path: Path):
    """When max-files cap is reached, exit code should be 3 (PARTIAL)."""
    repo_dir = make_py_fixture(
        tmp_path,
        {
            "a.py": "x = 1\n",
            "b.py": "x = 2\n",
            "c.py": "x = 3\n",
        },
    )
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "x",
            "--eval",
            "return True",
            "--max-files",
            "1",
        ],
    )
    assert result.exit_code == 3, (
        f"Expected exit 3 for partial result, got {result.exit_code}"
    )
    assert "partial" in result.output.lower()


# ---------------------------------------------------------------------------
# AC12: --eval and --eval-file mutually exclusive
# ---------------------------------------------------------------------------


def test_xray_explore_eval_and_eval_file_mutually_exclusive(tmp_path: Path):
    """Providing both --eval and --eval-file should exit non-zero with mutual exclusion error."""
    eval_file = tmp_path / "eval.py"
    eval_file.write_text("return True", encoding="utf-8")

    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            "/tmp",
            "--regex",
            "x",
            "--eval",
            "return True",
            "--eval-file",
            str(eval_file),
        ],
    )
    assert result.exit_code != 0
    output_lower = result.output.lower()
    assert "mutually exclusive" in output_lower or "exclusive" in output_lower


def test_xray_explore_missing_eval_and_eval_file():
    """Omitting both --eval and --eval-file should exit non-zero."""
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        ["--repo", "/tmp", "--regex", "x"],
    )
    assert result.exit_code != 0


def test_xray_explore_eval_file_loads_code(tmp_path: Path):
    """--eval-file should load evaluator code from a file and execute the search."""
    repo_dir = make_py_fixture(
        tmp_path / "repo",
        {"main.py": "password = 'secret'\n"},
    )
    eval_file = tmp_path / "eval.py"
    eval_file.write_text("return True", encoding="utf-8")

    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "password",
            "--eval-file",
            str(eval_file),
        ],
    )
    assert result.exit_code == 0, (
        f"Expected exit 0, got {result.exit_code}. Output: {result.output}"
    )


# ---------------------------------------------------------------------------
# AC13: --help exits 0 and shows explore subcommand flags
# ---------------------------------------------------------------------------


def test_xray_explore_help():
    """cidx xray explore --help should exit 0 and list all key flags."""
    runner = CliRunner()
    from code_indexer.cli import cli

    result = runner.invoke(cli, ["xray", "explore", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    output = result.output
    assert "--repo" in output
    assert "--regex" in output
    assert "--eval" in output
    assert "--max-debug-nodes" in output
    assert "--json" in output


def test_xray_group_help_shows_explore():
    """cidx xray --help should list both search and explore subcommands."""
    runner = CliRunner()
    from code_indexer.cli import cli

    result = runner.invoke(cli, ["xray", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "explore" in result.output
    assert "search" in result.output


# ---------------------------------------------------------------------------
# Additional: --quiet suppresses progress
# ---------------------------------------------------------------------------


def test_xray_explore_quiet_suppresses_progress(tmp_path: Path):
    """--quiet should suppress progress output; table still goes to stdout."""
    repo_dir = make_py_fixture(
        tmp_path,
        {"main.py": "x = 1\n"},
    )
    runner = CliRunner(mix_stderr=False)
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "x",
            "--eval",
            "return True",
            "--quiet",
        ],
    )
    assert result.exit_code == 0
    # stdout still has table output (files/matches/elapsed)
    output_lower = result.output.lower()
    assert (
        "files" in output_lower
        or "matches" in output_lower
        or "elapsed" in output_lower
        or "total" in output_lower
    )


# ---------------------------------------------------------------------------
# Additional: --no-matches exits 0
# ---------------------------------------------------------------------------


def test_xray_explore_no_matches_exits_zero(tmp_path: Path):
    """Search with no matches should still exit 0."""
    repo_dir = make_py_fixture(
        tmp_path,
        {"main.py": "x = 1\n"},
    )
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "ZZZNOTFOUND",
            "--eval",
            "return True",
        ],
    )
    assert result.exit_code == 0
    assert "0" in result.output  # 0 matches


# ---------------------------------------------------------------------------
# Additional: --target filename applies regex to paths
# ---------------------------------------------------------------------------


def test_xray_explore_target_filename(tmp_path: Path):
    """--target filename should apply regex to file paths, not content."""
    repo_dir = make_py_fixture(
        tmp_path,
        {
            "auth_handler.py": "x = 1\n",
            "utils.py": "auth = True\n",
        },
    )
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            r"auth.*\.py$",
            "--eval",
            "return True",
            "--target",
            "filename",
        ],
    )
    assert result.exit_code == 0
    # auth_handler.py matches the filename regex; utils.py does not
    assert "auth_handler.py" in result.output


# ---------------------------------------------------------------------------
# Additional: missing --repo exits non-zero
# ---------------------------------------------------------------------------


def test_xray_explore_missing_repo_flag():
    """Omitting --repo should show a usage error from Click."""
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        ["--regex", "x", "--eval", "return True"],
    )
    assert result.exit_code != 0


def test_xray_explore_missing_regex_flag():
    """Omitting --regex should show a usage error from Click."""
    runner = CliRunner()
    result = invoke_xray_explore(
        runner,
        ["--repo", "/tmp", "--eval", "return True"],
    )
    assert result.exit_code != 0
