"""History of the internal catalog is admin-only on every MCP git tool.

Real git repositories (the catalog's history includes a since-deleted
description of an ungranted repository), a real alias registry, a real
global-repos registry, a real ActivatedRepoManager holding an activation
of the catalog under a custom alias, and the real group store. The lock-in
test is parameterised over TOOL_REGISTRY, so a git tool added later is
covered automatically. Neutral names only.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import HANDLER_REGISTRY, _utils
from code_indexer.server.mcp.tools import TOOL_REGISTRY
from code_indexer.server.services.cidx_meta_access.history_policy import (
    CatalogSurface,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

from .conftest import extract_mcp_data

CATALOG_CLONE = "team-notes"
GIT_TOOLS = sorted(name for name in TOOL_REGISTRY if name.startswith("git_"))
GIT_REFUSAL = CatalogSurface.GIT_HISTORY.refusal

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Sample Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Sample Author",
    "GIT_COMMITTER_EMAIL": "author@example.com",
}


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, check=True, capture_output=True
    )
    return done.stdout.decode()


def _make_repo(path: Path, files: Dict[str, str], delete_after: List[str]) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    for name, text in files.items():
        (path / name).write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "describe repositories")
    for name in delete_after:
        (path / name).unlink()
    if delete_after:
        _git(path, "commit", "-q", "-am", "retire descriptions")


def _user(name: str, role: UserRole) -> User:
    return User(
        username=name, password_hash="hash", role=role, created_at=datetime.now()
    )


def _activate(data_dir: Path, source: Path, username: str, alias: str) -> None:
    user_dir = data_dir / "activated-repos" / username
    user_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", str(source), str(user_dir / alias)],
        env=_GIT_ENV,
        check=True,
        capture_output=True,
    )
    metadata = {
        "user_alias": alias,
        "golden_repo_alias": "cidx-meta",
        "current_branch": "main",
        "activated_at": "2026-01-01T00:00:00+00:00",
        "last_accessed": "2026-01-01T00:00:00+00:00",
    }
    (user_dir / f"{alias}_metadata.json").write_text(json.dumps(metadata))


@pytest.fixture(scope="module")
def module_tmp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("meta-history")


@pytest.fixture(scope="module")
def fx(module_tmp: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(module_tmp)


@pytest.fixture(scope="module")
def server(module_tmp: Path, fx: CidxMetaFixture) -> Iterator[SimpleNamespace]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield _build_server(module_tmp, fx, monkeypatch)


def _build_server(
    tmp_path: Path, fx: CidxMetaFixture, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    catalog = tmp_path / "sources" / "cidx-meta"
    _make_repo(
        catalog,
        {f"{ALPHA}.md": f"# {ALPHA}\n", f"{CANARY}.md": f"# {CANARY} internals\n"},
        delete_after=[f"{CANARY}.md"],
    )
    alpha = tmp_path / "sources" / ALPHA
    _make_repo(alpha, {"main.py": "print('alpha')\n"}, delete_after=[])

    golden_dir = tmp_path / "server" / "data" / "golden-repos"
    aliases = golden_dir / "aliases"
    aliases.mkdir(parents=True)
    manager = AliasManager(str(aliases))
    manager.create_alias("cidx-meta-global", str(catalog))
    manager.create_alias(f"{ALPHA}-global", str(alpha))
    for name, source in (("cidx-meta", catalog), (ALPHA, alpha)):
        fx.registry.register_repo(
            alias_name=f"{name}-global",
            repo_name=name,
            repo_url=f"https://git.example.com/{name}.git",
            index_path=str(source),
        )

    server_dir = tmp_path / "server"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(server_dir))
    data_dir = server_dir / "data"
    for username in (RESTRICTED_USER, ADMIN_USER):
        _activate(data_dir, catalog, username, CATALOG_CLONE)
    arm = ActivatedRepoManager(data_dir=str(data_dir))

    state = SimpleNamespace(
        golden_repos_dir=str(golden_dir),
        backend_registry=SimpleNamespace(global_repos=fx.registry),
        access_filtering_service=fx.access_service,
        activated_repo_manager=arm,
        payload_cache=None,
    )
    app_module = SimpleNamespace(
        app=SimpleNamespace(state=state),
        activated_repo_manager=arm,
        golden_repo_manager=None,
    )
    monkeypatch.setattr(_utils, "app_module", app_module)
    return SimpleNamespace(catalog=catalog, state=state)


def _dummy(prop: Dict[str, Any], name: str) -> Any:
    kind = prop.get("type")
    if isinstance(kind, list):
        kind = kind[0]
    if kind == "array":
        return [f"{ALPHA}.md"]
    if kind == "integer":
        return 1
    if kind == "boolean":
        return False
    if kind == "object":
        return {}
    if name in ("path", "file_path"):
        return f"{ALPHA}.md"
    if "commit" in name or "revision" in name:
        return "HEAD"
    return "sample"


_VALID_ARGS: Dict[str, Dict[str, Any]] = {
    "git_stash": {"action": "list"},
    "git_reset": {"mode": "mixed"},
    "git_search_diffs": {"search_string": CANARY},
    "git_search_commits": {"query": "describe"},
}

# Tools that refuse EVERY golden repository before resolving one; on a
# -global alias they never reach the history resolver.
_REFUSES_GOLDEN_BEFORE_RESOLUTION = {
    "git_branch_switch": "Cannot use git_branch_switch on golden repositories",
}


def _args_for(tool: str, alias: Any) -> Dict[str, Any]:
    schema = TOOL_REGISTRY[tool].get("inputSchema", {})
    props = schema.get("properties", {})
    args = {
        name: _dummy(props.get(name, {}), name) for name in schema.get("required", [])
    }
    args.update(_VALID_ARGS.get(tool, {}))
    args["repository_alias"] = alias
    return args


def _call(tool: str, args: Dict[str, Any], user: User) -> Dict[str, Any]:
    result = HANDLER_REGISTRY[tool](args, user)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return extract_mcp_data(result)


def test_lock_in_covers_the_git_tools() -> None:
    assert len(GIT_TOOLS) >= 20
    for name in ("git_log", "git_show_commit", "git_blame", "git_diff"):
        assert name in GIT_TOOLS
    assert all(name in HANDLER_REGISTRY for name in GIT_TOOLS)


@pytest.mark.parametrize("alias", ["cidx-meta-global", CATALOG_CLONE])
@pytest.mark.parametrize("tool", GIT_TOOLS)
def test_every_git_tool_refuses_catalog_history_for_restricted_caller(
    server: SimpleNamespace, tool: str, alias: str
) -> None:
    # Role ADMIN passes the tools' role gates; outside the admins group the
    # caller is still restricted.
    user = _user(RESTRICTED_USER, UserRole.ADMIN)
    data = _call(tool, _args_for(tool, alias), user)
    assert data.get("success") is False, data
    golden_refusal = _REFUSES_GOLDEN_BEFORE_RESOLUTION.get(tool)
    if golden_refusal is not None and alias.endswith("-global"):
        assert data["error"].startswith(golden_refusal), data
    else:
        assert data.get("error") == GIT_REFUSAL, data
    assert CANARY not in json.dumps(data)


@pytest.mark.parametrize("alias", ["cidx-meta-global", CATALOG_CLONE])
@pytest.mark.parametrize(
    "tool", ["git_log", "git_search_diffs", "git_file_at_revision", "git_show_commit"]
)
def test_admin_reads_catalog_history(
    server: SimpleNamespace, tool: str, alias: str
) -> None:
    args = _args_for(tool, alias)
    if tool == "git_search_diffs":
        args["search_string"] = CANARY
    if tool == "git_file_at_revision":
        args["path"] = f"{CANARY}.md"
        args["revision"] = "HEAD~1"
    data = _call(tool, args, _user(ADMIN_USER, UserRole.ADMIN))
    assert data.get("success") is True, data
    assert data.get("error") != GIT_REFUSAL


def test_role_admin_outside_admins_group_is_refused(server: SimpleNamespace) -> None:
    data = _call(
        "git_log",
        _args_for("git_log", "cidx-meta-global"),
        _user(RESTRICTED_USER, UserRole.ADMIN),
    )
    assert data.get("error") == GIT_REFUSAL


def test_omni_commit_search_keeps_other_repositories_and_refuses_the_catalog(
    server: SimpleNamespace,
) -> None:
    tool = "git_search_commits"
    args = _args_for(tool, ["cidx-meta-global", f"{ALPHA}-global"])
    data = _call(tool, args, _user(RESTRICTED_USER, UserRole.NORMAL_USER))
    assert data["errors"] == {"cidx-meta-global": GIT_REFUSAL}, data
    assert {m["source_repo"] for m in data["matches"]} == {f"{ALPHA}-global"}
    assert CANARY not in json.dumps(data)

    admin = _call(tool, args, _user(ADMIN_USER, UserRole.ADMIN))
    assert admin["errors"] == {}, admin
    assert {m["source_repo"] for m in admin["matches"]} == {
        "cidx-meta-global",
        f"{ALPHA}-global",
    }


TEMPORAL_REFUSAL = CatalogSurface.TEMPORAL.refusal
# The fixture app has no background job manager: a temporal search that
# passes the catalog policy stops at this check, before any job exists.
NO_JOB_SERVICE = "Background job/payload cache service not available"


def _temporal(alias: str, user: User) -> Dict[str, Any]:
    args = {"repository_alias": alias, "query_text": "retired", "time_range_all": True}
    return _call("search_code", args, user)


@pytest.mark.parametrize("alias", ["cidx-meta-global", "cidx-meta", CATALOG_CLONE])
def test_temporal_search_on_catalog_is_rejected_before_any_job(
    server: SimpleNamespace, alias: str
) -> None:
    data = _temporal(alias, _user(RESTRICTED_USER, UserRole.ADMIN))
    assert data["success"] is False
    assert data["error"] == TEMPORAL_REFUSAL, data
    assert CANARY not in json.dumps(data)


@pytest.mark.parametrize("alias", ["cidx-meta-global", CATALOG_CLONE])
def test_admin_temporal_search_on_catalog_passes_the_policy(
    server: SimpleNamespace, alias: str
) -> None:
    data = _temporal(alias, _user(ADMIN_USER, UserRole.ADMIN))
    assert data["error"] == NO_JOB_SERVICE, data


def test_temporal_search_on_granted_repository_is_not_rejected(
    server: SimpleNamespace,
) -> None:
    data = _temporal(f"{ALPHA}-global", _user(RESTRICTED_USER, UserRole.NORMAL_USER))
    assert data["error"] == NO_JOB_SERVICE, data


XRAY_REFUSAL = CatalogSurface.XRAY.refusal
# Every X-Ray tool that runs over a repository. store_xray_pattern only
# stores a pattern in the catalog and is not search.
XRAY_TOOLS = sorted(
    {n for n in TOOL_REGISTRY if "xray" in n and n != "store_xray_pattern"}
    | {"analyze_graph"}
)
# Tools that refuse a whole list naming the catalog. xray_dump_ast takes one
# repository; xray_search_batch refuses per alias and continues with the
# others (test_xray_batch_reports_the_catalog_as_a_per_alias_error).
XRAY_LIST_TOOLS = [
    t for t in XRAY_TOOLS if t not in ("xray_dump_ast", "xray_search_batch")
]
_XRAY_ARGS: Dict[str, Dict[str, Any]] = {
    "xray_search": {"pattern": "describe", "search_target": "content"},
    "xray_explore": {"pattern": "describe"},
    "xray_dump_ast": {"file_path": f"{ALPHA}.md"},
    "xray_search_batch": {"scans": [{"driver_regex": "describe"}]},
    "analyze_graph": {"pattern": "describe", "evaluator_code": "true"},
}


def _xray_args(tool: str, alias: Any) -> Dict[str, Any]:
    args = _args_for(tool, alias)
    args.update(_XRAY_ARGS.get(tool, {}))
    args["repository_alias"] = alias
    return args


def _assert_xray_refused(data: Dict[str, Any]) -> None:
    text = json.dumps(data)
    assert XRAY_REFUSAL in text, data
    assert CANARY not in text
    assert "job_id" not in data and "job_ids" not in data


def test_xray_lock_in_covers_the_xray_tools() -> None:
    expected = {"xray_search", "xray_explore", "xray_dump_ast", "xray_search_batch"}
    assert expected <= set(XRAY_TOOLS)
    assert "store_xray_pattern" not in XRAY_TOOLS
    assert all(name in HANDLER_REGISTRY for name in XRAY_TOOLS)


@pytest.mark.parametrize("alias", ["cidx-meta-global", "cidx-meta"])
@pytest.mark.parametrize("tool", XRAY_TOOLS)
def test_every_xray_tool_refuses_the_catalog_for_restricted_caller(
    server: SimpleNamespace, tool: str, alias: str
) -> None:
    user = _user(RESTRICTED_USER, UserRole.NORMAL_USER)
    _assert_xray_refused(_call(tool, _xray_args(tool, alias), user))


@pytest.mark.parametrize("tool", XRAY_TOOLS)
def test_role_admin_outside_admins_group_is_refused_xray(
    server: SimpleNamespace, tool: str
) -> None:
    user = _user(RESTRICTED_USER, UserRole.ADMIN)
    _assert_xray_refused(_call(tool, _xray_args(tool, "cidx-meta-global"), user))


@pytest.mark.parametrize("tool", XRAY_LIST_TOOLS)
def test_xray_list_naming_the_catalog_refuses_it(
    server: SimpleNamespace, tool: str
) -> None:
    aliases = ["cidx-meta-global", f"{ALPHA}-global"]
    user = _user(RESTRICTED_USER, UserRole.NORMAL_USER)
    _assert_xray_refused(_call(tool, _xray_args(tool, aliases), user))


def test_xray_batch_reports_the_catalog_as_a_per_alias_error(
    server: SimpleNamespace,
) -> None:
    tool = "xray_search_batch"
    user = _user(RESTRICTED_USER, UserRole.NORMAL_USER)
    data = _call(tool, _xray_args(tool, ["cidx-meta-global", "cidx-meta"]), user)
    per_alias = {e["repository_alias"]: e for e in data["errors"]}
    assert per_alias["cidx-meta-global"]["message"] == XRAY_REFUSAL, data
    assert per_alias["cidx-meta-global"]["error_level"] == "repo"
    assert per_alias["cidx-meta"]["message"] == XRAY_REFUSAL
    assert "job_id" not in data


@pytest.mark.parametrize("tool", XRAY_TOOLS)
def test_admin_xray_on_the_catalog_is_not_refused(
    server: SimpleNamespace, tool: str
) -> None:
    try:
        data = _call(
            tool,
            _xray_args(tool, "cidx-meta-global"),
            _user(ADMIN_USER, UserRole.ADMIN),
        )
    except RuntimeError as exc:
        # The fixture wires no X-Ray executor: reaching it means the
        # catalog policy let the admin through.
        data = {"infrastructure": str(exc)}
    assert XRAY_REFUSAL not in json.dumps(data), data
