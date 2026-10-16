"""Repo-wide structural guard: every server `cidx index` spawn supplies a
stats attribution (Story #2118 slice 1a).

``append_server_layout_args`` (server/utils/index_command_layout.py) is the
single seam every server-context `cidx index` command goes through (enforced
by test_index_command_layout_spawn_site_guard_1488.py). It REQUIRES a
keyword-only ``attribution`` and stamps it as ``--stats-attribution=<json>``,
so every embedding_call_stats row the child writes carries golden_repo_alias,
job_id and node_id.

This guard discovers every call of that helper under src/code_indexer (no
hardcoded list) and fails on any call that does not pass ``attribution=`` or
passes the literal ``None``. (The 1488 guard file is already over the
1000-line limit, so this check lives here.)
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List

import pytest

_TARGET_FUNCTION = "append_server_layout_args"
_SRC_ROOT = Path(__file__).resolve().parents[4] / "src" / "code_indexer"
#: The helper's own module defines it; it never calls it.
_DEFINING_MODULE = "server/utils/index_command_layout.py"
#: Lower bound on discovered calls: the server spawn sites present when this
#: guard was written (refresh_scheduler 2, golden_repo_manager 6,
#: activated_repo_index_manager 3, mcp repos 1, temporal cmd builder 1,
#: claude_cli_manager 1).
_MIN_EXPECTED_CALLS = 14


def _callee_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls_missing_attribution(source: str) -> List[int]:
    """Line numbers of helper calls with no usable ``attribution=``."""
    missing: List[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call) and _callee_name(node.func) == _TARGET_FUNCTION
        ):
            continue
        attribution = next(
            (kw.value for kw in node.keywords if kw.arg == "attribution"), None
        )
        if attribution is None or (
            isinstance(attribution, ast.Constant) and attribution.value is None
        ):
            missing.append(node.lineno)
    return missing


def _count_calls(source: str) -> int:
    return sum(
        1
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and _callee_name(node.func) == _TARGET_FUNCTION
    )


def _discover_calling_files() -> Dict[str, str]:
    found: Dict[str, str] = {}
    for path in _SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        if rel == _DEFINING_MODULE:
            continue
        source = path.read_text()
        if _TARGET_FUNCTION in source and _count_calls(source):
            found[rel] = source
    return found


_CALLING_FILES: Dict[str, str] = _discover_calling_files()


@pytest.mark.parametrize("rel_path", sorted(_CALLING_FILES))
def test_every_spawn_site_supplies_a_stats_attribution(rel_path: str) -> None:
    missing = _calls_missing_attribution(_CALLING_FILES[rel_path])
    assert not missing, (
        f"{rel_path}: {_TARGET_FUNCTION}(...) at line(s) {missing} passes no "
        "attribution (Story #2118). Pass attribution=build_server_stats_"
        "attribution(alias=..., job_id=<job id or None>, site=..., purpose="
        "'index'|'refresh') so the child's embedding rows carry alias, job "
        "and node."
    )


def test_discovery_is_non_vacuous() -> None:
    total = sum(_count_calls(src) for src in _CALLING_FILES.values())
    assert total >= _MIN_EXPECTED_CALLS, (
        f"found only {total} {_TARGET_FUNCTION}(...) calls under {_SRC_ROOT}; "
        f"expected at least {_MIN_EXPECTED_CALLS} -- discovery is broken"
    )


#: An attributed child writes rows only when its env carries the stats
#: bootstrap dir, merged by this ONE helper (embedding_stats_child_wiring).
_STATS_ENV_BUILDER = "build_embedding_stats_child_env"
#: Shared runners that merge the stats env for every command they spawn;
#: test_stats_env_runners_really_merge_it proves each one does.
_STATS_ENV_RUNNERS: Dict[str, str] = {
    "_run_subprocess_with_telemetry": "server/services/activated_repo_index_manager.py",
    "_run_provider_subprocess": "server/mcp/handlers/repos.py",
}
#: Functions that only BUILD a command for a caller to spawn.
_COMMAND_BUILDERS: Dict[str, str] = {
    "_build_temporal_index_cmd": (
        "returns the command; _provider_temporal_job spawns it through "
        "repos._run_provider_subprocess"
    ),
}


def _outermost_defs(tree: ast.Module) -> List[ast.AST]:
    defs: List[ast.AST] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append(node)
        elif isinstance(node, ast.ClassDef):
            defs.extend(
                n
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return defs


def _referenced_names(node: ast.AST) -> set:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
        elif isinstance(sub, ast.alias):
            names.add(sub.name)
    return names


def _spawn_defs_without_stats_env(source: str) -> List[str]:
    """Outermost functions that call the helper but whose child env cannot
    carry the stats bootstrap dir."""
    missing: List[str] = []
    for fn in _outermost_defs(ast.parse(source)):
        if not _count_calls(ast.unparse(fn)):
            continue
        names = _referenced_names(fn)
        if (
            _STATS_ENV_BUILDER in names
            or names & set(_STATS_ENV_RUNNERS)
            or getattr(fn, "name", "") in _COMMAND_BUILDERS
        ):
            continue
        missing.append(getattr(fn, "name", "?"))
    return missing


@pytest.mark.parametrize("rel_path", sorted(_CALLING_FILES))
def test_every_spawn_site_child_can_write_stats_rows(rel_path: str) -> None:
    missing = _spawn_defs_without_stats_env(_CALLING_FILES[rel_path])
    assert not missing, (
        f"{rel_path}: {missing} spawn an attributed `cidx index` whose env "
        f"never gets {_STATS_ENV_BUILDER}(...) (Story #2118): the child "
        "installs a NoOpWriter and its embedding_call_stats rows are lost."
    )


def test_stats_env_runners_really_merge_it() -> None:
    for runner, rel_path in _STATS_ENV_RUNNERS.items():
        [fn] = [
            f
            for f in ast.walk(ast.parse((_SRC_ROOT / rel_path).read_text()))
            if isinstance(f, ast.FunctionDef) and f.name == runner
        ]
        assert _STATS_ENV_BUILDER in _referenced_names(fn), (rel_path, runner)


def test_stats_env_detection_has_teeth() -> None:
    bare = (
        "def spawn(a):\n"
        "    subprocess.run(append_server_layout_args(['cidx', 'index'], "
        "attribution=a), env=build_cidx_subprocess_env())\n"
    )
    merged = bare.replace(
        "build_cidx_subprocess_env()",
        "build_cidx_subprocess_env(build_embedding_stats_child_env(cfg))",
    )
    assert _spawn_defs_without_stats_env(bare) == ["spawn"]
    assert _spawn_defs_without_stats_env(merged) == []


def test_attribution_detection_has_teeth() -> None:
    missing_kw = "cmd = append_server_layout_args(['cidx', 'index'])\n"
    none_kw = "cmd = append_server_layout_args(['cidx', 'index'], attribution=None)\n"
    supplied = (
        "cmd = append_server_layout_args(['cidx', 'index'], attribution=attr)\n"
        "cmd2 = layout.append_server_layout_args(base, attribution=make())\n"
    )
    assert _calls_missing_attribution(missing_kw) == [1]
    assert _calls_missing_attribution(none_kw) == [1]
    assert _calls_missing_attribution(supplied) == []
