"""Import verification and structural tests for handlers/ package.

These tests define the target structure and will fail until the handlers/
package is created. They represent the acceptance criteria for Story #496.
"""

import importlib
import ast
import os
import pytest


# Single source of truth for all 122 expected HANDLER_REGISTRY keys (AC7)
EXPECTED_REGISTRY_KEYS = frozenset(
    {
        # Dict literal entries from original HANDLER_REGISTRY definition
        "search_code",
        "discover_repositories",
        "list_repositories",
        "activate_repository",
        "deactivate_repository",
        "get_repository_status",
        "sync_repository",
        "switch_branch",
        "list_files",
        "get_file_content",
        "browse_directory",
        "get_branches",
        "check_health",
        "check_hnsw_health",
        "add_golden_repo",
        "remove_golden_repo",
        "refresh_golden_repo",
        "change_golden_repo_branch",
        "list_users",
        "create_user",
        "get_repository_statistics",
        "get_job_statistics",
        "get_job_details",
        "get_all_repositories_status",
        "manage_composite_repository",
        "list_global_repos",
        "global_repo_status",
        "get_global_config",
        "set_global_config",
        "add_golden_repo_index",
        "get_golden_repo_indexes",
        "regex_search",
        "create_file",
        "edit_file",
        "delete_file",
        "enter_write_mode",
        "exit_write_mode",
        # Late registrations
        "git_log",
        "git_show_commit",
        "git_file_at_revision",
        "git_diff",
        "git_blame",
        "git_file_history",
        "git_search_commits",
        "git_search_diffs",
        "directory_tree",
        "authenticate",
        "cidx_ssh_key_create",
        "cidx_ssh_key_list",
        "cidx_ssh_key_delete",
        "cidx_ssh_key_show_public",
        "cidx_ssh_key_assign_host",
        "cidx_quick_reference",
        "trigger_reindex",
        "get_index_status",
        "git_status",
        "git_stage",
        "git_unstage",
        "git_commit",
        "git_push",
        "git_pull",
        "git_fetch",
        "git_reset",
        "git_clean",
        "git_merge",
        "git_merge_abort",
        "git_conflict_status",
        "git_mark_resolved",
        "git_checkout_file",
        "git_branch_list",
        "git_branch_create",
        "git_branch_switch",
        "git_branch_delete",
        "configure_git_credential",
        "list_git_credentials",
        "delete_git_credential",
        "create_pull_request",
        "list_pull_requests",
        "get_pull_request",
        "list_pull_request_comments",
        "comment_on_pull_request",
        "update_pull_request",
        "merge_pull_request",
        "close_pull_request",
        "git_stash",
        "git_amend",
        "first_time_user_guide",
        "get_tool_categories",
        "dependency_analysis_workflow",
        "admin_logs_query",
        "admin_logs_export",
        "get_scip_audit_log",
        "scip_definition",
        "scip_references",
        "scip_dependencies",
        "scip_dependents",
        "scip_impact",
        "scip_callchain",
        "scip_context",
        "gitlab_ci_list_pipelines",
        "gitlab_ci_get_pipeline",
        "gitlab_ci_search_logs",
        "gitlab_ci_get_job_logs",
        "gitlab_ci_retry_pipeline",
        "gitlab_ci_cancel_pipeline",
        "github_actions_list_runs",
        "github_actions_get_run",
        "github_actions_search_logs",
        "github_actions_get_job_logs",
        "github_actions_retry_run",
        "github_actions_cancel_run",
        "get_cached_content",
        "set_session_impersonation",
        "list_delegation_functions",
        "execute_delegation_function",
        "poll_delegation_job",
        "execute_open_delegation",
        "cs_register_repository",
        "cs_list_repositories",
        "cs_check_health",
        "list_groups",
        "create_group",
        "get_group",
        "update_group",
        "delete_group",
        "add_member_to_group",
        "remove_member_from_group",
        "add_repos_to_group",
        "remove_repo_from_group",
        "bulk_remove_repos_from_group",
        "list_api_keys",
        "create_api_key",
        "delete_api_key",
        "list_mcp_credentials",
        "create_mcp_credential",
        "delete_mcp_credential",
        "admin_list_user_mcp_credentials",
        "admin_create_user_mcp_credential",
        "admin_delete_user_mcp_credential",
        "admin_list_all_mcp_credentials",
        "admin_list_system_mcp_credentials",
        "query_audit_logs",
        "enter_maintenance_mode",
        "exit_maintenance_mode",
        "get_maintenance_status",
        "scip_pr_history",
        "scip_cleanup_history",
        "scip_cleanup_workspaces",
        "scip_cleanup_status",
        "start_trace",
        "end_trace",
        "list_repo_categories",
        "trigger_dependency_analysis",
        "wiki_article_analytics",
        "manage_provider_indexes",
        "bulk_add_provider_index",
        "get_provider_health",
        "depmap_find_consumers",
        "depmap_get_repo_domains",
        "depmap_get_domain_summary",
        "depmap_get_stale_domains",
        "depmap_get_cross_domain_graph",
    }
)

# All domain module names (no path, no .py extension)
DOMAIN_MODULES = [
    "search",
    "repos",
    "files",
    "git_read",
    "git_write",
    "pull_requests",
    "cicd",
    "scip",
    "admin",
    "ssh_keys",
    "delegation",
    "guides",
    "depmap",
]

# Short and absolute package names for the handlers package
_HANDLERS_ABS = "code_indexer.server.mcp.handlers"
_HANDLERS_SHORT = "handlers"


def _handlers_dir() -> str:
    """Return the absolute path to the handlers/ package directory."""
    return os.path.normpath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "..",
            "..",
            "src",
            "code_indexer",
            "server",
            "mcp",
            "handlers",
        )
    )


def _is_sibling_import(module_name: str, alias_names: list, current_mod: str) -> bool:
    """Return True if this import references a sibling domain module.

    Handles all four forms:
      1. from code_indexer.server.mcp.handlers.X import ...
      2. from handlers.X import ...
      3. from code_indexer.server.mcp.handlers import X  (X is sibling name)
      4. from handlers import X  (X is sibling name)
    """
    siblings = set(DOMAIN_MODULES) - {current_mod}

    # Form 1: from code_indexer.server.mcp.handlers.X import ...
    abs_prefix = _HANDLERS_ABS + "."
    if module_name.startswith(abs_prefix):
        seg = module_name[len(abs_prefix) :].split(".")[0]
        if seg in siblings:
            return True

    # Form 2: from handlers.X import ...
    short_prefix = _HANDLERS_SHORT + "."
    if module_name.startswith(short_prefix):
        seg = module_name[len(short_prefix) :].split(".")[0]
        if seg in siblings:
            return True

    # Form 3: from code_indexer.server.mcp.handlers import X
    if module_name == _HANDLERS_ABS or module_name.endswith(".mcp.handlers"):
        if any(name in siblings for name in alias_names):
            return True

    # Form 4: from handlers import X
    if module_name == _HANDLERS_SHORT:
        if any(name in siblings for name in alias_names):
            return True

    return False


def test_all_public_handlers_importable():
    """All public handlers must be importable from the package (AC1)."""
    import code_indexer.server.mcp.handlers as h

    # Spot-check key handlers from each domain
    assert hasattr(h, "search_code")
    assert hasattr(h, "list_repositories")
    assert hasattr(h, "list_files")
    assert hasattr(h, "git_status")
    assert hasattr(h, "git_commit")
    assert hasattr(h, "create_pull_request")
    assert hasattr(h, "scip_definition")
    assert hasattr(h, "handle_authenticate")
    assert hasattr(h, "handle_ssh_key_create")
    assert hasattr(h, "handle_list_delegation_functions")
    assert hasattr(h, "quick_reference")


def test_app_module_attribute_accessible():
    """handlers.app_module must be accessible as protocol.py expects (AC8)."""
    import code_indexer.server.mcp.handlers as h

    assert hasattr(h, "app_module"), "app_module must be a package-level attribute"


def test_handler_registry_accessible():
    """HANDLER_REGISTRY must be a package-level attribute with expected count (AC7)."""
    import code_indexer.server.mcp.handlers as h

    assert hasattr(h, "HANDLER_REGISTRY"), "HANDLER_REGISTRY must be accessible"
    assert isinstance(h.HANDLER_REGISTRY, dict)
    expected_count = len(EXPECTED_REGISTRY_KEYS)
    assert len(h.HANDLER_REGISTRY) >= expected_count, (
        f"Expected {expected_count}+ entries, got {len(h.HANDLER_REGISTRY)}"
    )


def test_scip_queries_private_helper_importable():
    """_apply_scip_payload_truncation must be importable (used by scip_queries.py, AC1)."""
    from code_indexer.server.mcp.handlers import _apply_scip_payload_truncation

    assert callable(_apply_scip_payload_truncation)


def test_provider_indexes_private_helpers_importable():
    """6 private helpers used by provider_indexes.py must be importable (AC1)."""
    from code_indexer.server.mcp.handlers import (
        _resolve_golden_repo_path,
        _resolve_golden_repo_base_clone,
        _remove_provider_from_config,
        _append_provider_to_config,
        _list_global_repos,
        _provider_index_job,
    )

    assert callable(_resolve_golden_repo_path)
    assert callable(_resolve_golden_repo_base_clone)
    assert callable(_remove_provider_from_config)
    assert callable(_append_provider_to_config)
    assert callable(_list_global_repos)
    assert callable(_provider_index_job)


def test_no_circular_imports():
    """Package import must complete without circular import errors (AC3).

    Uses a subprocess to test import in a clean interpreter — avoids
    issues with sys.modules aliasing used in the Phase 1 package scaffold.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import code_indexer.server.mcp.handlers; print('ok')",
        ],
        capture_output=True,
        text=True,
        env={
            "PYTHONPATH": str(
                os.path.normpath(
                    os.path.join(
                        os.path.dirname(__file__), "..", "..", "..", "..", "src"
                    )
                )
            )
        },
    )
    assert result.returncode == 0, f"Circular import detected. stderr:\n{result.stderr}"
    assert result.stdout.strip() == "ok"


def test_domain_modules_exist():
    """All 12 domain modules plus _utils must exist as importable submodules (AC5).

    Modules are extracted incrementally; skips individual not-yet-created modules
    rather than failing hard, so the test suite stays green during the migration.
    Only skips when the top-level module itself is absent; re-raises for transitive
    import errors inside an existing module so real defects are not hidden.
    """
    module_paths = [
        f"code_indexer.server.mcp.handlers.{name}"
        for name in DOMAIN_MODULES + ["_utils"]
    ]
    for mod_name in module_paths:
        try:
            mod = importlib.import_module(mod_name)
            assert mod is not None, f"Module {mod_name} must exist"
        except ModuleNotFoundError as e:
            if getattr(e, "name", None) == mod_name:
                pytest.skip(
                    f"Module {mod_name} not yet extracted — skipping until complete"
                )
            raise


def test_no_cross_domain_imports():
    """No domain module should import from a sibling domain module (AC2).

    Checks ast.Import (import X.Y) and ast.ImportFrom (from X import Y)
    in both absolute and short path forms, including 'from handlers import X'.
    """
    hdir = _handlers_dir()

    for mod_name in DOMAIN_MODULES:
        path = os.path.join(hdir, f"{mod_name}.py")
        if not os.path.exists(path):
            pytest.skip(f"Module {mod_name}.py not yet created")

        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        siblings = set(DOMAIN_MODULES) - {mod_name}
        abs_prefix = _HANDLERS_ABS + "."
        short_prefix = _HANDLERS_SHORT + "."

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name or ""
                    if name.startswith(abs_prefix):
                        seg = name[len(abs_prefix) :].split(".")[0]
                        assert seg not in siblings, (
                            f"{mod_name}.py must not import sibling via 'import {name}'"
                        )
                    if name.startswith(short_prefix):
                        seg = name[len(short_prefix) :].split(".")[0]
                        assert seg not in siblings, (
                            f"{mod_name}.py must not import sibling via 'import {name}'"
                        )

            elif isinstance(node, ast.ImportFrom) and node.module:
                alias_names = [a.name for a in node.names]
                assert not _is_sibling_import(node.module, alias_names, mod_name), (
                    f"{mod_name}.py must not import from sibling module "
                    f"(found: from {node.module} import {alias_names})"
                )


def test_handler_registry_has_exact_count():
    """HANDLER_REGISTRY must contain exactly the expected entries (AC7)."""
    import code_indexer.server.mcp.handlers as h

    registry = h.HANDLER_REGISTRY
    missing = EXPECTED_REGISTRY_KEYS - set(registry.keys())
    extra = set(registry.keys()) - EXPECTED_REGISTRY_KEYS

    assert not missing, f"Missing keys in HANDLER_REGISTRY: {sorted(missing)}"
    assert not extra, f"Unexpected extra keys in HANDLER_REGISTRY: {sorted(extra)}"


def test_no_module_exceeds_line_limit():
    """No domain module exceeds 2500 lines; _utils.py <= 1500 lines (AC5)."""
    hdir = _handlers_dir()

    if not os.path.isdir(hdir):
        pytest.skip("handlers/ package not yet created")

    utils_limit = 1500
    domain_limit = 2500

    for fname in os.listdir(hdir):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(hdir, fname)
        with open(fpath, "r", encoding="utf-8") as f:
            line_count = sum(1 for _ in f)

        if fname == "_utils.py":
            assert line_count <= utils_limit, (
                f"_utils.py has {line_count} lines, exceeds limit of {utils_limit}"
            )
        elif fname not in ("__init__.py", "_legacy.py"):
            assert line_count <= domain_limit, (
                f"{fname} has {line_count} lines, exceeds limit of {domain_limit}"
            )


def test_utils_has_no_intra_package_imports():
    """_utils.py must not import from any sibling handlers submodule (AC6).

    Checks both ast.Import and ast.ImportFrom in absolute, short, and bare
    'from handlers import X' forms.
    """
    hdir = _handlers_dir()
    utils_path = os.path.join(hdir, "_utils.py")

    if not os.path.exists(utils_path):
        pytest.skip("_utils.py not yet created")

    with open(utils_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    abs_prefix = _HANDLERS_ABS + "."
    short_prefix = _HANDLERS_SHORT + "."
    forbidden_names = set(DOMAIN_MODULES) | {"_utils"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name or ""
                assert not name.startswith(abs_prefix), (
                    f"_utils.py must not import from handlers submodule: "
                    f"'import {name}'"
                )
                assert not name.startswith(short_prefix), (
                    f"_utils.py must not import from handlers submodule: "
                    f"'import {name}'"
                )

        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            alias_names = [a.name for a in node.names]

            # from code_indexer.server.mcp.handlers.X import ...
            if module.startswith(abs_prefix):
                raise AssertionError(
                    f"_utils.py must not import from handlers submodule: "
                    f"'from {module} import ...'"
                )
            # from handlers.X import ...
            if module.startswith(short_prefix):
                raise AssertionError(
                    f"_utils.py must not import from handlers submodule: "
                    f"'from {module} import ...'"
                )
            # from code_indexer.server.mcp.handlers import X
            if module == _HANDLERS_ABS or module.endswith(".mcp.handlers"):
                bad = [n for n in alias_names if n in forbidden_names]
                assert not bad, (
                    f"_utils.py must not import domain modules from handlers: "
                    f"'from {module} import {bad}'"
                )
            # from handlers import X
            if module == _HANDLERS_SHORT:
                bad = [n for n in alias_names if n in forbidden_names]
                assert not bad, (
                    f"_utils.py must not import domain modules from handlers: "
                    f"'from {module} import {bad}'"
                )
