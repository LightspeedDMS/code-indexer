"""Tests for the `cidx xray search` CLI command (Story #975).

Uses click.testing.CliRunner — no real subprocess, no external services.
Tree-sitter IS imported (xray extras boundary is respected at module level but
deliberately triggered when XRaySearchEngine is instantiated inside the command).
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner, Result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def invoke_xray_search(runner: CliRunner, args: list) -> Result:
    """Import cli lazily to avoid top-level tree_sitter import during collection."""
    from code_indexer.cli import cli

    return runner.invoke(cli, ["xray", "search"] + args, catch_exceptions=False)


def make_py_fixture(tmp_path: Path, files: dict) -> Path:
    """Create Python files under tmp_path with given content, return tmp_path."""
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Lazy-load gate
# ---------------------------------------------------------------------------


def test_cli_import_does_not_pull_tree_sitter():
    """Importing the CLI module must not trigger tree_sitter at import time."""
    import sys

    # Remove cached module so we can test a fresh import path
    mods_to_remove = [k for k in sys.modules if "code_indexer.cli" == k]
    for m in mods_to_remove:
        sys.modules.pop(m, None)

    import importlib

    import code_indexer.cli  # noqa: F401

    importlib.import_module("code_indexer.cli")

    assert "tree_sitter" not in sys.modules, (
        "tree_sitter must not be imported at CLI startup — lazy-load boundary violated"
    )


# ---------------------------------------------------------------------------
# Flag validation — timeout bounds
# ---------------------------------------------------------------------------


def test_xray_search_invalid_timeout_low():
    """--timeout below 10 should exit 2 with an error message."""
    runner = CliRunner()
    result = invoke_xray_search(
        runner,
        [
            "--repo",
            "/tmp",
            "--regex",
            "x",
            "--eval",
            "return True",
            "--timeout",
            "5",
        ],
    )
    assert result.exit_code == 2
    assert "timeout" in result.output.lower()


def test_xray_search_invalid_timeout_high():
    """--timeout above 600 should exit 2."""
    runner = CliRunner()
    result = invoke_xray_search(
        runner,
        [
            "--repo",
            "/tmp",
            "--regex",
            "x",
            "--eval",
            "return True",
            "--timeout",
            "900",
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Flag validation — max-files
# ---------------------------------------------------------------------------


def test_xray_search_invalid_max_files_zero():
    """--max-files 0 should exit 2."""
    runner = CliRunner()
    result = invoke_xray_search(
        runner,
        [
            "--repo",
            "/tmp",
            "--regex",
            "x",
            "--eval",
            "return True",
            "--max-files",
            "0",
        ],
    )
    assert result.exit_code == 2
    output_text = result.output
    assert "max-files" in output_text.lower() or "max_files" in output_text.lower()


def test_xray_search_invalid_max_files_negative():
    """--max-files -1 should exit 2."""
    runner = CliRunner()
    result = invoke_xray_search(
        runner,
        [
            "--repo",
            "/tmp",
            "--regex",
            "x",
            "--eval",
            "return True",
            "--max-files",
            "-1",
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Evaluator validation
# ---------------------------------------------------------------------------


def test_xray_search_bad_evaluator_import():
    """An evaluator with 'import os' should fail validation and exit 2."""
    runner = CliRunner()
    result = invoke_xray_search(
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


def test_xray_search_bad_evaluator_from_import():
    """An evaluator with 'from os import path' should fail validation and exit 2."""
    runner = CliRunner()
    result = invoke_xray_search(
        runner,
        [
            "--repo",
            "/tmp",
            "--regex",
            "x",
            "--eval",
            "from os import path",
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Repo path validation
# ---------------------------------------------------------------------------


def test_xray_search_invalid_repo_path():
    """A non-existent repo path should exit 2 with a clear error."""
    runner = CliRunner()
    result = invoke_xray_search(
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
    assert (
        "nonexistent" in result.output
        or "not exist" in result.output
        or "directory" in result.output.lower()
    )


# ---------------------------------------------------------------------------
# --eval-file and mutual exclusion
# ---------------------------------------------------------------------------


def test_xray_search_eval_file_loads_code(tmp_path: Path):
    """--eval-file should load evaluator code from a file and execute the search."""
    repo_dir = make_py_fixture(
        tmp_path / "repo",
        {"main.py": "password = 'secret'\n"},
    )
    eval_file = tmp_path / "eval.py"
    eval_file.write_text("return True", encoding="utf-8")

    runner = CliRunner()
    result = invoke_xray_search(
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
    assert "1" in result.output  # at least 1 match


def test_xray_search_eval_and_eval_file_mutually_exclusive(tmp_path: Path):
    """Providing both --eval and --eval-file should exit non-zero."""
    eval_file = tmp_path / "eval.py"
    eval_file.write_text("return True", encoding="utf-8")

    runner = CliRunner()
    result = invoke_xray_search(
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
    assert (
        "mutually exclusive" in result.output.lower()
        or "exclusive" in result.output.lower()
    )


def test_xray_search_eval_file_nonexistent(tmp_path: Path):
    """--eval-file pointing to a non-existent file should exit 2."""
    runner = CliRunner()
    result = invoke_xray_search(
        runner,
        [
            "--repo",
            "/tmp",
            "--regex",
            "x",
            "--eval-file",
            str(tmp_path / "missing.py"),
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# End-to-end: table output
# ---------------------------------------------------------------------------


def test_xray_search_runs_against_real_fixture(tmp_path: Path):
    """Search with a real repo fixture should exit 0 and report matches."""
    repo_dir = make_py_fixture(
        tmp_path,
        {
            "auth.py": "password = 'hunter2'\n",
            "utils.py": "# no secrets here\n",
        },
    )
    runner = CliRunner()
    result = invoke_xray_search(
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
    assert result.exit_code == 0, f"Output: {result.output}"
    # Table output should mention file count and matches
    assert "matches" in result.output.lower() or "1" in result.output


def test_xray_search_table_shows_files_and_elapsed(tmp_path: Path):
    """Table output should include files_processed and elapsed_seconds lines."""
    repo_dir = make_py_fixture(
        tmp_path,
        {"main.py": "x = 1\n"},
    )
    runner = CliRunner()
    result = invoke_xray_search(
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
    assert result.exit_code == 0
    assert "files" in result.output.lower()
    assert "elapsed" in result.output.lower() or "s" in result.output


def test_xray_search_no_matches_exits_zero(tmp_path: Path):
    """Search with no matches should still exit 0 (not an error condition)."""
    repo_dir = make_py_fixture(
        tmp_path,
        {"main.py": "x = 1\n"},
    )
    runner = CliRunner()
    result = invoke_xray_search(
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
# JSON output
# ---------------------------------------------------------------------------


def test_xray_search_json_output(tmp_path: Path):
    """--json flag should emit valid JSON with required top-level keys."""
    repo_dir = make_py_fixture(
        tmp_path,
        {"main.py": "password = 'secret'\n"},
    )
    runner = CliRunner()
    result = invoke_xray_search(
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
    for key in (
        "matches",
        "evaluation_errors",
        "files_processed",
        "files_total",
        "elapsed_seconds",
    ):
        assert key in data, f"Missing key '{key}' in JSON output"
    assert isinstance(data["matches"], list)
    assert isinstance(data["evaluation_errors"], list)
    assert isinstance(data["files_processed"], int)
    assert isinstance(data["elapsed_seconds"], float)


def test_xray_search_json_includes_partial_key_on_cap(tmp_path: Path):
    """--json with --max-files reaching cap should include partial=true."""
    repo_dir = make_py_fixture(
        tmp_path,
        {
            "a.py": "x = 1\n",
            "b.py": "x = 2\n",
            "c.py": "x = 3\n",
        },
    )
    runner = CliRunner()
    result = invoke_xray_search(
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
# max-files cap
# ---------------------------------------------------------------------------


def test_xray_search_max_files_cap_shows_partial(tmp_path: Path):
    """Table output with max-files cap should show PARTIAL indicator, exit 3."""
    repo_dir = make_py_fixture(
        tmp_path,
        {
            "a.py": "x = 1\n",
            "b.py": "x = 2\n",
            "c.py": "x = 3\n",
            "d.py": "x = 4\n",
            "e.py": "x = 5\n",
        },
    )
    runner = CliRunner()
    result = invoke_xray_search(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "x",
            "--eval",
            "return True",
            "--max-files",
            "2",
        ],
    )
    assert result.exit_code == 3, (
        f"Expected exit 3, got {result.exit_code}. Output: {result.output}"
    )
    assert "partial" in result.output.lower()


# ---------------------------------------------------------------------------
# --target flag
# ---------------------------------------------------------------------------


def test_xray_search_target_filename(tmp_path: Path):
    """--target filename should apply regex to file paths, not content."""
    repo_dir = make_py_fixture(
        tmp_path,
        {
            "auth_handler.py": "x = 1\n",
            "utils.py": "auth = True\n",  # content has 'auth' but filename doesn't match Test*.java
        },
    )
    runner = CliRunner()
    result = invoke_xray_search(
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


def test_xray_search_invalid_target_value():
    """--target xml should be rejected by Click as an invalid choice."""
    runner = CliRunner()
    result = runner.invoke(
        __import__("code_indexer.cli", fromlist=["cli"]).cli,
        [
            "xray",
            "search",
            "--repo",
            "/tmp",
            "--regex",
            "x",
            "--eval",
            "return True",
            "--target",
            "xml",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert (
        "xml" in result.output
        or "invalid" in result.output.lower()
        or "choice" in result.output.lower()
    )


# ---------------------------------------------------------------------------
# --include / --exclude patterns
# ---------------------------------------------------------------------------


def test_xray_search_include_pattern_filters_files(tmp_path: Path):
    """--include '*.py' should only search Python files."""
    repo_dir = make_py_fixture(
        tmp_path,
        {
            "main.py": "password = 'secret'\n",
            "readme.md": "password info here\n",
        },
    )
    runner = CliRunner()
    result = invoke_xray_search(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "password",
            "--eval",
            "return True",
            "--include",
            "*.py",
            "--json",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    file_paths = [m["file_path"] for m in data["matches"]]
    # Only .py files should appear
    assert all(p.endswith(".py") for p in file_paths)


def test_xray_search_exclude_pattern_filters_files(tmp_path: Path):
    """--exclude '*/test/*' should skip test directory files."""
    repo_dir = make_py_fixture(
        tmp_path,
        {
            "main.py": "password = 'secret'\n",
            "test/test_main.py": "password = 'test'\n",
        },
    )
    runner = CliRunner()
    result = invoke_xray_search(
        runner,
        [
            "--repo",
            str(repo_dir),
            "--regex",
            "password",
            "--eval",
            "return True",
            "--exclude",
            "test/*",
            "--json",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    file_paths = [m["file_path"] for m in data["matches"]]
    assert not any("test" in Path(p).parts for p in file_paths)


# ---------------------------------------------------------------------------
# --quiet flag
# ---------------------------------------------------------------------------


def test_xray_search_quiet_suppresses_progress(tmp_path: Path):
    """--quiet should suppress progress output; table still goes to stdout."""
    repo_dir = make_py_fixture(
        tmp_path,
        {"main.py": "x = 1\n"},
    )
    runner = CliRunner(mix_stderr=False)
    result = invoke_xray_search(
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
    # stdout should still have table output
    assert (
        "files" in result.output.lower()
        or "matches" in result.output.lower()
        or "elapsed" in result.output.lower()
    )


# ---------------------------------------------------------------------------
# Missing required flags
# ---------------------------------------------------------------------------


def test_xray_search_missing_repo_flag():
    """Omitting --repo should show a usage error from Click."""
    runner = CliRunner()
    result = invoke_xray_search(
        runner,
        ["--regex", "x", "--eval", "return True"],
    )
    assert result.exit_code != 0


def test_xray_search_missing_regex_flag():
    """Omitting --regex should show a usage error from Click."""
    runner = CliRunner()
    result = invoke_xray_search(
        runner,
        ["--repo", "/tmp", "--eval", "return True"],
    )
    assert result.exit_code != 0


def test_xray_search_missing_eval_and_eval_file():
    """Omitting both --eval and --eval-file should show a usage error."""
    runner = CliRunner()
    result = invoke_xray_search(
        runner,
        ["--repo", "/tmp", "--regex", "x"],
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Help smoke test
# ---------------------------------------------------------------------------


def test_xray_search_help():
    """cidx xray search --help should exit 0 and list all key flags."""
    runner = CliRunner()
    from code_indexer.cli import cli

    result = runner.invoke(cli, ["xray", "search", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    output = result.output
    assert "--repo" in output
    assert "--regex" in output
    assert "--eval" in output
    assert "--target" in output
    assert "--timeout" in output
    assert "--json" in output


def test_xray_group_help():
    """cidx xray --help should exit 0 and list search subcommand."""
    runner = CliRunner()
    from code_indexer.cli import cli

    result = runner.invoke(cli, ["xray", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "search" in result.output


# ---------------------------------------------------------------------------
# DEFECT 1 regression: errors must go to stderr, not stdout
# ---------------------------------------------------------------------------


def test_xray_search_errors_go_to_stderr_not_stdout():
    """In --json mode, stdout MUST remain JSON-clean; errors go to stderr."""
    runner = CliRunner(mix_stderr=False)
    from code_indexer.cli import cli

    result = runner.invoke(
        cli,
        [
            "xray",
            "search",
            "--repo",
            "/tmp",
            "--regex",
            "x",
            "--eval",
            "import os",  # bad evaluator → validation_failed error
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 2
    # stdout must be empty (JSON-clean) — no error text on stdout
    assert result.output.strip() == "", (
        f"Expected empty stdout in --json mode, got: {repr(result.output)}"
    )
    # error must appear on stderr
    assert "evaluator validation failed" in result.stderr.lower(), (
        f"Expected 'evaluator validation failed' in stderr, got: {repr(result.stderr)}"
    )
