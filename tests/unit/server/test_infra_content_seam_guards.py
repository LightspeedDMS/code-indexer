"""Guards behind the infra rows of the content-seam inventory (WP5).

Each test pins the fact a ``local-only`` / ``area: activation`` row's note
relies on, so the classification fails loudly if the code under it changes:

* the auto-updater's CoW symlink topology (``DeploymentExecutor``) runs only in
  the bare-metal systemd auto-update unit, never in the server/container;
* ``langfuse_*`` golden repos are ``local://`` and pod-local by design: their
  trace files are written under this pod's data dir and indexed pod-locally;
* functions with no production caller stay that way;
* ``activated-repos/<user>`` permission helpers serve activation only.
"""

from __future__ import annotations

import ast
import functools
import re
from pathlib import Path
from typing import Iterator, List, Tuple
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src" / "code_indexer"


@functools.lru_cache(maxsize=1)
def _parsed_sources() -> Tuple[Tuple[str, ast.AST], ...]:
    parsed = []
    for path in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:  # removed while the tree was being walked
            continue
        rel = path.relative_to(_SRC).as_posix()
        parsed.append((rel, ast.parse(text, filename=rel)))
    return tuple(parsed)


def _python_sources() -> Iterator[Tuple[str, ast.AST]]:
    yield from _parsed_sources()


def _modules_referencing(name: str) -> List[str]:
    """Modules whose code (not docstrings/comments) mentions *name* as an
    attribute or a bare name."""
    found = []
    for rel, tree in _python_sources():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == name) or (
                isinstance(node, ast.Name) and node.id == name
            ):
                found.append(rel)
                break
    return found


def _modules_calling(name: str) -> List[str]:
    found = []
    for rel, tree in _python_sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if called == name:
                found.append(rel)
                break
    return found


# ---------------------------------------------------------------------------
# Auto-update: bare-metal systemd only
# ---------------------------------------------------------------------------

_SYMLINK_METHODS = (
    "_ensure_golden_repos_symlink_for_cow_daemon",
    "_ensure_activated_repos_symlink_for_cow_daemon",
    "_create_golden_repos_symlink",
    "_migrate_real_dir_to_cow_symlink",
    "_reconcile_existing_cow_symlink",
    "_remove_if_empty_dir",
)


def test_deployment_executor_is_constructed_only_by_the_auto_update_unit() -> None:
    assert _modules_calling("DeploymentExecutor") == ["server/auto_update/run_once.py"]


def test_cow_symlink_topology_methods_are_private_to_the_executor() -> None:
    for method in _SYMLINK_METHODS:
        assert _modules_referencing(method) == [
            "server/auto_update/deployment_executor.py"
        ], method


def test_auto_update_runs_from_systemd_and_the_container_runs_the_server() -> None:
    unit = (
        _SRC / "server" / "auto_update" / "templates" / "cidx-auto-update.service"
    ).read_text()
    assert re.search(
        r"^ExecStart=.*-m code_indexer\.server\.auto_update\.run_once\b", unit, re.M
    )
    dockerfile = (_ROOT / "Dockerfile").read_text()
    cmd = [line for line in dockerfile.splitlines() if line.startswith("CMD ")]
    assert cmd and "code_indexer.server.main" in cmd[-1]
    assert "auto_update" not in dockerfile


def test_server_imports_only_constants_and_parsers_from_the_executor() -> None:
    """lifespan/config_service/routes/applied_worker_count reach into
    deployment_executor for paths and ExecStart parsing, never a symlink step."""
    allowed = {
        "LAUNCH_CONFIG_PATH",
        "APPLIED_LAUNCH_CONFIG_PATH",
        "RESTART_SIGNAL_PATH",
        "read_execstart_flags",
        "DeploymentExecutor",  # applied_worker_count: static ExecStart parsers
    }
    for rel, tree in _python_sources():
        if rel.startswith("server/auto_update/"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                "auto_update.deployment_executor"
            ):
                names = {alias.name for alias in node.names}
                assert names <= allowed, (rel, names - allowed)


def test_applied_worker_count_uses_only_static_parsers() -> None:
    path = _SRC / "server" / "services" / "applied_worker_count.py"
    tree = ast.parse(path.read_text())
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "DeploymentExecutor"
    }
    assert used == {"_is_cidx_execstart", "_read_flag"}


# ---------------------------------------------------------------------------
# Langfuse: pod-local local:// repos
# ---------------------------------------------------------------------------


def test_langfuse_folders_register_as_local_repos(tmp_path: Path) -> None:
    from code_indexer.server.startup.bootstrap import register_langfuse_golden_repos

    (tmp_path / "langfuse_proj_user").mkdir()
    (tmp_path / "other-repo").mkdir()
    manager = MagicMock()
    manager.register_local_repo.return_value = False
    register_langfuse_golden_repos(manager, str(tmp_path))
    manager.register_local_repo.assert_called_once_with(
        alias="langfuse_proj_user",
        folder_path=tmp_path / "langfuse_proj_user",
        fire_lifecycle_hooks=False,
    )


def test_register_local_repo_stamps_a_local_url() -> None:
    from code_indexer.server.repositories import golden_repo_manager

    source = Path(golden_repo_manager.__file__).read_text()
    tree = ast.parse(source)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "register_local_repo"
    )
    segment = ast.get_source_segment(source, fn)
    assert segment is not None
    assert 'repo_url=f"local://{alias}"' in segment


def test_langfuse_repos_are_pod_local_for_indexing() -> None:
    from code_indexer.server.utils.index_command_layout import (
        append_server_layout_args,
        is_pod_local_repo,
        repo_layout_kwargs,
    )

    url, alias = "local://langfuse_proj_user", "langfuse_proj_user"
    assert is_pod_local_repo(url, alias)
    kwargs = repo_layout_kwargs(url, alias)
    assert kwargs == {"local_content": True}
    cmd = append_server_layout_args(
        ["cidx", "index"],
        content_backend="delegated",
        git_backend="delegated",
        **kwargs,
    )
    assert not any("delegated" in arg for arg in cmd)


def test_trace_files_land_under_this_pods_data_dir(tmp_path: Path) -> None:
    from code_indexer.server.services.langfuse_trace_sync_service import (
        LangfuseTraceSyncService,
    )

    service = LangfuseTraceSyncService(
        config_getter=MagicMock(), data_dir=str(tmp_path)
    )
    folder = service._get_trace_folder("proj", {"userId": "u", "sessionId": "s"})
    assert folder == tmp_path / "golden-repos" / "langfuse_proj_u" / "s"


# ---------------------------------------------------------------------------
# No production caller
# ---------------------------------------------------------------------------


def test_static_readme_extraction_has_no_caller() -> None:
    """RepoAnalyzer's static README/manifest chain is dead since v10.4.13
    (extract_info never falls back to it)."""
    assert _modules_calling("_extract_info_static") == []


def test_meta_description_hook_find_readme_has_no_caller() -> None:
    callers = [
        rel
        for rel, tree in _python_sources()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_find_readme"
    ]
    assert callers == []


def test_nfs_check_path_accessible_has_no_caller() -> None:
    assert _modules_calling("check_path_accessible") == []


# ---------------------------------------------------------------------------
# activated-repos/<user> permissions: activation only
# ---------------------------------------------------------------------------


def test_user_dir_permissions_serve_activation_only() -> None:
    assert _modules_calling("ensure_activated_user_dir") == [
        "server/repositories/activated_repo_manager.py"
    ]
    for helper in ("_ensure_primary_group", "_repair_existing_user_dir"):
        assert _modules_calling(helper) == [
            "server/repositories/user_dir_permissions.py"
        ], helper


# ── golden-repos root: cidx's own state on a mount-less pod ────────────────

_GOLDEN_ROOT_TAG = re.compile(r"^golden-repos root \(cidx state\): `([^`]+)` -- ")


def _code_strings(module: str) -> List[str]:
    """String constants in *module*'s code -- docstrings excluded."""
    tree = ast.parse((_SRC / module).read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in docstrings
    ]


def _golden_root_rows() -> List[dict]:
    import json

    rows = json.loads(
        (_ROOT / "tests/unit/server/content_seam_inventory.json").read_text()
    )
    return [r for r in rows if _GOLDEN_ROOT_TAG.match(r["note"])]


def test_golden_root_rows_are_cidx_state_named_by_their_module() -> None:
    """A row reclassified as cidx's own state on the golden-repos root names the
    cidx-owned location it touches (``.locks``, ``.scratch``, ``aliases``,
    ``.versioned``, ...), and its module's code names that same location. If
    the module stops naming it, the path may now come from elsewhere -- the
    classification must be re-decided."""
    rows = _golden_root_rows()
    assert len(rows) >= 77
    for row in rows:
        match = _GOLDEN_ROOT_TAG.match(row["note"])
        assert match is not None
        location = match.group(1)
        assert row["class"] == "cidx-state", row
        assert any(location in s for s in _code_strings(row["module"])), (
            f"{row['module']} no longer names {location!r}: re-decide {row}"
        )


def test_golden_repos_root_is_under_cidx_data_dir() -> None:
    """The golden-repos root is the server's own data directory, not a
    repository: GoldenRepoManager roots it at ``<data_dir>/golden-repos``."""
    source = (_SRC / "server/repositories/golden_repo_manager.py").read_text()
    assert (
        'self.golden_repos_dir = os.path.join(self.data_dir, "golden-repos")' in source
    )
