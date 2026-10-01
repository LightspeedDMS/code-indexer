"""Bug #1549: service_init.py must wire acquire_primary_instance_lock()
into the two startup orphan-cleanup call sites.

initialize_services() is not practically unit-testable in isolation (it
performs real DB/filesystem bootstrap), so this is an AST-based structural
guard -- the same technique this codebase already uses for
service_init.py/lifespan.py ordering invariants (see
test_lifespan_backend_registry_wiring_1515.py). It parses the real source
and verifies actual data flow: the SAME variable assigned from
acquire_primary_instance_lock() is passed as is_primary_instance= into
both destructive-sweep call sites, and both occur after the acquisition.
"""

import ast
from pathlib import Path
from typing import Tuple


def _load_module_ast() -> ast.Module:
    src_path = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "code_indexer"
        / "server"
        / "startup"
        / "service_init.py"
    )
    return ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))


def _find_calls(tree: ast.Module, callee_name: str):
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            )
            if name == callee_name:
                calls.append(node)
    return calls


def _find_lock_assignment(tree: ast.Module) -> Tuple[str, int]:
    """Find the sole `<var> = acquire_primary_instance_lock(...)`
    assignment and return (var_name, lineno). Fails loudly if there is
    not exactly one, or its target is not a single plain name."""
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "acquire_primary_instance_lock"
    ]
    assert len(matches) == 1, (
        "service_init.py must assign the result of "
        "acquire_primary_instance_lock(...) to a variable exactly once "
        f"(Bug #1549), found {len(matches)}"
    )
    assign = matches[0]
    assert len(assign.targets) == 1 and isinstance(assign.targets[0], ast.Name), (
        "acquire_primary_instance_lock(...) result must be assigned to a "
        "single plain variable name"
    )
    return assign.targets[0].id, assign.lineno


def _kwarg_value_name(call: ast.Call, kwarg: str):
    for kw in call.keywords:
        if kw.arg == kwarg and isinstance(kw.value, ast.Name):
            return kw.value.id
    return None


class TestPrimaryInstanceLockWiredIntoServiceInit:
    def test_lock_result_threaded_into_job_tracker_cleanup_call(self) -> None:
        tree = _load_module_ast()
        var_name, assign_lineno = _find_lock_assignment(tree)

        calls = _find_calls(tree, "cleanup_orphaned_jobs_on_startup")
        assert len(calls) == 1, (
            f"expected exactly one cleanup_orphaned_jobs_on_startup() call "
            f"site, found {len(calls)}"
        )
        cleanup_call = calls[0]
        assert cleanup_call.lineno > assign_lineno, (
            "cleanup_orphaned_jobs_on_startup() must run AFTER "
            "acquire_primary_instance_lock() (Bug #1549)"
        )
        assert _kwarg_value_name(cleanup_call, "is_primary_instance") == var_name, (
            "cleanup_orphaned_jobs_on_startup() must pass "
            f"is_primary_instance={var_name} (Bug #1549)"
        )

    def test_lock_result_threaded_into_background_job_manager_construction(
        self,
    ) -> None:
        tree = _load_module_ast()
        var_name, assign_lineno = _find_lock_assignment(tree)

        calls = _find_calls(tree, "BackgroundJobManager")
        assert len(calls) == 1, (
            f"expected exactly one BackgroundJobManager(...) construction "
            f"call site, found {len(calls)}"
        )
        bjm_call = calls[0]
        assert bjm_call.lineno > assign_lineno, (
            "BackgroundJobManager(...) must be constructed AFTER "
            "acquire_primary_instance_lock() (Bug #1549)"
        )
        assert _kwarg_value_name(bjm_call, "is_primary_instance") == var_name, (
            f"BackgroundJobManager(...) must pass is_primary_instance="
            f"{var_name} (Bug #1549)"
        )


def _find_log_calls_with_message_substring(tree: ast.Module, substring: str):
    """Return (level, call) for every logger.<level>(...) call whose first
    positional argument's constant/f-string text contains `substring`."""
    matches = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        ):
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        text_parts = []
        for sub in ast.walk(first_arg):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                text_parts.append(sub.value)
        joined = " ".join(text_parts)
        if substring in joined:
            matches.append((node.func.attr, node))
    return matches


class TestPrimaryInstanceLockSkipLogLevelIsNotWarning:
    """Bug #1549 Finding 2b (Codex-confirmed): under `uvicorn --workers N`,
    every worker runs initialize_services(); exactly one acquires the
    primary-instance lock and the other N-1 correctly, routinely take the
    non-primary path -- entirely expected on EVERY multi-worker startup,
    not a signal of a caller bug. The pre-fix WARNING here is not in
    LOG_AUDIT_ALLOWLIST, so a normal multi-worker startup failed the
    mandatory post-E2E log-audit gate (Phase 3/4). Per the established
    Bug #1535 precedent (demote a happy-path, by-design log line to
    DEBUG rather than allowlisting it), this is demoted. Not practically
    unit-testable via execution (initialize_services() performs real
    DB/filesystem bootstrap), so this is a source-level structural check,
    matching this file's existing AST-based approach."""

    def test_non_primary_skip_message_is_not_logged_via_warning(self) -> None:
        tree = _load_module_ast()
        matches = _find_log_calls_with_message_substring(
            tree, "could not acquire the primary-instance lock"
        )
        assert matches, (
            "expected to find the Bug #1549 non-primary-instance skip log "
            "call in service_init.py -- source text may have changed"
        )
        warning_matches = [level for level, _call in matches if level == "warning"]
        assert warning_matches == [], (
            "the non-primary-instance skip message must not be logged via "
            f"logger.warning (routine multi-worker startup, Bug #1549 "
            f"Finding 2b), found levels: {[lvl for lvl, _ in matches]}"
        )
