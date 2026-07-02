"""X-Ray CLI commands (Story #975).

Provides the ``cidx xray`` group and ``cidx xray search`` subcommand for
direct, blocking execution of an X-Ray two-phase AST search against a local
repository.  No server required; no async job ID.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click


# ---------------------------------------------------------------------------
# xray group
# ---------------------------------------------------------------------------


@click.group("xray")
def xray_group() -> None:
    """AST-aware code search via tree-sitter."""
    pass


# ---------------------------------------------------------------------------
# xray search command
# ---------------------------------------------------------------------------


@xray_group.command("search")
@click.option(
    "--repo", "repo", required=True, help="Local path to repository to search."
)
@click.option(
    "--regex",
    "driver_regex",
    required=True,
    help="Phase 1 regex driver applied to files.",
)
@click.option(
    "--eval",
    "evaluator_code",
    default=None,
    help="Phase 2 Python evaluator code (inline). Mutually exclusive with --eval-file.",
)
@click.option(
    "--eval-file",
    "eval_file",
    default=None,
    type=click.Path(exists=False, file_okay=True, dir_okay=False),
    help="Path to a file containing Phase 2 Python evaluator code. Mutually exclusive with --eval.",
)
@click.option(
    "--target",
    "search_target",
    type=click.Choice(["content", "filename"]),
    default="content",
    show_default=True,
    help="What Phase 1 regex applies to: file content or filename.",
)
@click.option(
    "--include",
    "include_patterns",
    multiple=True,
    help="Glob patterns to include (repeatable). Empty = include all.",
)
@click.option(
    "--exclude",
    "exclude_patterns",
    multiple=True,
    help="Glob patterns to exclude (repeatable).",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=120,
    show_default=True,
    help="Wall-clock cap in seconds (10-600).",
)
@click.option(
    "--max-files",
    "max_files",
    type=int,
    default=None,
    help="Cap number of files evaluated. Reaching cap exits 3 and marks PARTIAL.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit JSON instead of a formatted table.",
)
@click.option(
    "--quiet",
    "quiet",
    is_flag=True,
    default=False,
    help="Suppress progress output to stderr. Table/JSON still goes to stdout.",
)
def xray_search(
    repo: str,
    driver_regex: str,
    evaluator_code: Optional[str],
    eval_file: Optional[str],
    search_target: str,
    include_patterns: tuple,
    exclude_patterns: tuple,
    timeout_seconds: int,
    max_files: Optional[int],
    output_json: bool,
    quiet: bool,
) -> None:
    """Run a two-phase X-Ray search synchronously and print results.

    Phase 1: regex driver selects candidate files (by content or filename).
    Phase 2: Python evaluator runs against each candidate's AST.

    Exit codes:
      0  -- success, all files evaluated.
      2  -- usage / validation error.
      3  -- partial result (timeout or max-files cap reached).
    """
    # ------------------------------------------------------------------
    # Mutual exclusion: --eval vs --eval-file
    # ------------------------------------------------------------------
    if evaluator_code is not None and eval_file is not None:
        click.echo("Error: --eval and --eval-file are mutually exclusive.", err=True)
        sys.exit(2)

    if evaluator_code is None and eval_file is None:
        click.echo("Error: one of --eval or --eval-file is required.", err=True)
        sys.exit(2)

    # ------------------------------------------------------------------
    # Load evaluator code from file if --eval-file was given
    # ------------------------------------------------------------------
    if eval_file is not None:
        eval_path = Path(eval_file)
        if not eval_path.is_file():
            click.echo(f"Error: --eval-file path does not exist: {eval_file}", err=True)
            sys.exit(2)
        evaluator_code = eval_path.read_text(encoding="utf-8")

    # mypy narrowing: evaluator_code is str from here onward
    assert evaluator_code is not None  # guaranteed by logic above

    # ------------------------------------------------------------------
    # Validate timeout
    # ------------------------------------------------------------------
    if not (10 <= timeout_seconds <= 600):
        click.echo(
            f"Error: --timeout must be between 10 and 600, got {timeout_seconds}.",
            err=True,
        )
        sys.exit(2)

    # ------------------------------------------------------------------
    # Validate max_files
    # ------------------------------------------------------------------
    if max_files is not None and max_files < 1:
        click.echo(
            f"Error: --max-files must be >= 1, got {max_files}.",
            err=True,
        )
        sys.exit(2)

    # ------------------------------------------------------------------
    # Validate repo path
    # ------------------------------------------------------------------
    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        click.echo(
            f"Error: repository path does not exist or is not a directory: {repo}",
            err=True,
        )
        sys.exit(2)

    # ------------------------------------------------------------------
    # Import XRaySearchEngine (tree-sitter is a core dependency)
    # ------------------------------------------------------------------
    from code_indexer.xray.search_engine import XRaySearchEngine

    engine = XRaySearchEngine()

    # ------------------------------------------------------------------
    # Pre-flight: validate evaluator code before touching the filesystem
    # ------------------------------------------------------------------
    validation = engine.sandbox.validate(evaluator_code)
    if not validation.ok:
        click.echo(
            f"Error: evaluator validation failed: {validation.reason}",
            err=True,
        )
        sys.exit(2)

    # ------------------------------------------------------------------
    # Progress callback (writes to stderr unless --quiet or --json)
    # JSON mode implicitly suppresses progress so stdout is parseable JSON.
    # ------------------------------------------------------------------
    suppress_progress = quiet or output_json

    def _progress(percent: int, phase: str, detail: str) -> None:
        if not suppress_progress:
            click.echo(f"[{percent:3d}%] {phase}: {detail}", err=True)

    # ------------------------------------------------------------------
    # Run the search
    # ------------------------------------------------------------------
    result = engine.run(
        repo_path=repo_path,
        driver_regex=driver_regex,
        evaluator_code=evaluator_code,
        search_target=search_target,
        include_patterns=list(include_patterns),
        exclude_patterns=list(exclude_patterns),
        timeout_seconds=timeout_seconds,
        max_files=max_files,
        progress_callback=_progress,
    )

    # ------------------------------------------------------------------
    # Determine exit code
    # ------------------------------------------------------------------
    is_partial = result.get("partial", False)
    exit_code = 3 if is_partial else 0

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    if output_json:
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        _print_table(result)

    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Table formatter (stdout only)
# ---------------------------------------------------------------------------


def _print_table(result: dict) -> None:
    """Print a human-readable table of X-Ray search results to stdout."""
    matches = result.get("matches", [])
    evaluation_errors = result.get("evaluation_errors", [])
    files_processed = result.get("files_processed", 0)
    files_total = result.get("files_total", 0)
    elapsed = result.get("elapsed_seconds", 0.0)
    is_partial = result.get("partial", False)
    max_files_reached = result.get("max_files_reached", False)

    if is_partial:
        if max_files_reached:
            click.echo("[PARTIAL: max_files reached]")
        else:
            click.echo("[PARTIAL: timeout]")

    click.echo(f"Files processed: {files_processed}/{files_total}")
    click.echo(f"Elapsed: {elapsed:.3f}s")
    click.echo(f"Matches: {len(matches)}")
    click.echo(f"Evaluation errors: {len(evaluation_errors)}")

    if matches:
        click.echo()
        click.echo("=== MATCHES ===")
        for m in matches:
            file_path = m.get("file_path", "")
            language = m.get("language", "")
            line_number = m.get("line_number")
            line_str = f"  line {line_number}" if line_number is not None else ""
            click.echo(f"  {file_path}  [{language}]{line_str}")

    if evaluation_errors:
        click.echo()
        click.echo("=== EVALUATION ERRORS ===")
        for e in evaluation_errors:
            file_path = e.get("file_path", "")
            error_type = e.get("error_type", "")
            error_message = e.get("error_message", "")
            click.echo(f"  {file_path}: {error_type}: {error_message}")


# ---------------------------------------------------------------------------
# xray explore command
# ---------------------------------------------------------------------------


@xray_group.command("explore")
@click.option(
    "--repo", "repo", required=True, help="Local path to repository to search."
)
@click.option(
    "--regex",
    "driver_regex",
    required=True,
    help="Phase 1 regex driver applied to files.",
)
@click.option(
    "--eval",
    "evaluator_code",
    default=None,
    help="Phase 2 Python evaluator code (inline). Mutually exclusive with --eval-file.",
)
@click.option(
    "--eval-file",
    "eval_file",
    default=None,
    type=click.Path(exists=False, file_okay=True, dir_okay=False),
    help="Path to a file containing Phase 2 Python evaluator code. Mutually exclusive with --eval.",
)
@click.option(
    "--target",
    "search_target",
    type=click.Choice(["content", "filename"]),
    default="content",
    show_default=True,
    help="What Phase 1 regex applies to: file content or filename.",
)
@click.option(
    "--include",
    "include_patterns",
    multiple=True,
    help="Glob patterns to include (repeatable). Empty = include all.",
)
@click.option(
    "--exclude",
    "exclude_patterns",
    multiple=True,
    help="Glob patterns to exclude (repeatable).",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=120,
    show_default=True,
    help="Wall-clock cap in seconds (10-600).",
)
@click.option(
    "--max-files",
    "max_files",
    type=int,
    default=None,
    help="Cap number of files evaluated. Reaching cap exits 3 and marks PARTIAL.",
)
@click.option(
    "--max-debug-nodes",
    "max_debug_nodes",
    type=click.IntRange(1, 500),
    default=50,
    show_default=True,
    help="Maximum AST nodes per match in AST debug output (1-500).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit JSON instead of formatted output.",
)
@click.option(
    "--quiet",
    "quiet",
    is_flag=True,
    default=False,
    help="Suppress progress output to stderr. Table/JSON still goes to stdout.",
)
def xray_explore(
    repo: str,
    driver_regex: str,
    evaluator_code: Optional[str],
    eval_file: Optional[str],
    search_target: str,
    include_patterns: tuple,
    exclude_patterns: tuple,
    timeout_seconds: int,
    max_files: Optional[int],
    max_debug_nodes: int,
    output_json: bool,
    quiet: bool,
) -> None:
    """Explore AST node breakdowns for matches without running an evaluator.

    Runs the same two-phase X-Ray search as ``xray search`` but includes a
    verbose AST subtree below each match to help iterate on evaluator
    expressions.

    Exit codes:
      0  -- success, all files evaluated.
      2  -- usage / validation error.
      3  -- partial result (timeout or max-files cap reached).
    """
    # ------------------------------------------------------------------
    # Mutual exclusion: --eval vs --eval-file
    # ------------------------------------------------------------------
    if evaluator_code is not None and eval_file is not None:
        click.echo("Error: --eval and --eval-file are mutually exclusive.", err=True)
        sys.exit(2)

    if evaluator_code is None and eval_file is None:
        click.echo("Error: one of --eval or --eval-file is required.", err=True)
        sys.exit(2)

    # ------------------------------------------------------------------
    # Load evaluator code from file if --eval-file was given
    # ------------------------------------------------------------------
    if eval_file is not None:
        eval_path = Path(eval_file)
        if not eval_path.is_file():
            click.echo(f"Error: --eval-file path does not exist: {eval_file}", err=True)
            sys.exit(2)
        evaluator_code = eval_path.read_text(encoding="utf-8")

    # mypy narrowing: evaluator_code is str from here onward
    assert evaluator_code is not None  # guaranteed by logic above

    # ------------------------------------------------------------------
    # Validate timeout
    # ------------------------------------------------------------------
    if not (10 <= timeout_seconds <= 600):
        click.echo(
            f"Error: --timeout must be between 10 and 600, got {timeout_seconds}.",
            err=True,
        )
        sys.exit(2)

    # ------------------------------------------------------------------
    # Validate max_files
    # ------------------------------------------------------------------
    if max_files is not None and max_files < 1:
        click.echo(
            f"Error: --max-files must be >= 1, got {max_files}.",
            err=True,
        )
        sys.exit(2)

    # ------------------------------------------------------------------
    # Validate repo path
    # ------------------------------------------------------------------
    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        click.echo(
            f"Error: repository path does not exist or is not a directory: {repo}",
            err=True,
        )
        sys.exit(2)

    # ------------------------------------------------------------------
    # Import XRaySearchEngine (tree-sitter is a core dependency)
    # ------------------------------------------------------------------
    from code_indexer.xray.search_engine import XRaySearchEngine

    engine = XRaySearchEngine()

    # ------------------------------------------------------------------
    # Pre-flight: validate evaluator code before touching the filesystem
    # ------------------------------------------------------------------
    validation = engine.sandbox.validate(evaluator_code)
    if not validation.ok:
        click.echo(
            f"Error: evaluator validation failed: {validation.reason}",
            err=True,
        )
        sys.exit(2)

    # ------------------------------------------------------------------
    # Progress callback (writes to stderr unless --quiet or --json)
    # JSON mode implicitly suppresses progress so stdout is parseable JSON.
    # ------------------------------------------------------------------
    suppress_progress = quiet or output_json

    def _progress(percent: int, phase: str, detail: str) -> None:
        if not suppress_progress:
            click.echo(f"[{percent:3d}%] {phase}: {detail}", err=True)

    # ------------------------------------------------------------------
    # Run the search with AST debug enabled
    # ------------------------------------------------------------------
    result = engine.run(
        repo_path=repo_path,
        driver_regex=driver_regex,
        evaluator_code=evaluator_code,
        search_target=search_target,
        include_patterns=list(include_patterns),
        exclude_patterns=list(exclude_patterns),
        timeout_seconds=timeout_seconds,
        max_files=max_files,
        include_ast_debug=True,
        max_debug_nodes=max_debug_nodes,
        progress_callback=_progress,
    )

    # ------------------------------------------------------------------
    # Determine exit code
    # ------------------------------------------------------------------
    is_partial = result.get("partial", False)
    exit_code = 3 if is_partial else 0

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    if output_json:
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        _render_explore(result)

    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Explore renderer (stdout only)
# ---------------------------------------------------------------------------


def _render_explore(result: dict) -> None:
    """Print a human-readable exploration report with AST subtrees to stdout."""
    matches = result.get("matches", [])
    evaluation_errors = result.get("evaluation_errors", [])
    files_processed = result.get("files_processed", 0)
    files_total = result.get("files_total", 0)
    elapsed = result.get("elapsed_seconds", 0.0)
    is_partial = result.get("partial", False)
    max_files_reached = result.get("max_files_reached", False)

    if is_partial:
        if max_files_reached:
            click.echo("[PARTIAL: max_files reached]")
        else:
            click.echo("[PARTIAL: timeout]")

    for m in matches:
        file_path = m.get("file_path", "")
        language = m.get("language", "")
        line_number = m.get("line_number")
        line_str = f"  line {line_number}" if line_number is not None else ""
        click.echo(f"  {file_path}  [{language}]{line_str}")

        ast_debug = m.get("ast_debug")
        if ast_debug:
            click.echo("  AST:")
            _print_ast(ast_debug, indent=4)
        click.echo()

    click.echo(f"Files processed: {files_processed}/{files_total}")
    click.echo(f"Elapsed: {elapsed:.3f}s")
    click.echo(f"Matches: {len(matches)}")
    click.echo(f"Evaluation errors: {len(evaluation_errors)}")

    if evaluation_errors:
        click.echo()
        click.echo("=== EVALUATION ERRORS ===")
        for e in evaluation_errors:
            fp = e.get("file_path", "")
            error_type = e.get("error_type", "")
            error_message = e.get("error_message", "")
            click.echo(f"  {fp}: {error_type}: {error_message}")


def _print_ast(node: dict, indent: int) -> None:
    """Recursively print an AST node dict with indentation.

    Args:
        node: Dict produced by XRaySearchEngine._serialize_ast. May contain a
            ``{"type": "...truncated"}`` sentinel child produced when the
            max_debug_nodes cap is reached.
        indent: Number of leading spaces for this node's line.
    """
    node_type = node.get("type", "")
    if node_type == "...truncated":
        click.echo(" " * indent + "...truncated")
        return

    start_point = node.get("start_point", [0, 0])
    end_point = node.get("end_point", [0, 0])
    text_preview = node.get("text_preview", "").replace("\n", "\\n")
    if len(text_preview) > 80:
        text_preview = text_preview[:80]

    click.echo(
        f"{' ' * indent}{node_type} [{start_point}..{end_point}] '{text_preview}'"
    )

    for child in node.get("children", []):
        _print_ast(child, indent + 2)
