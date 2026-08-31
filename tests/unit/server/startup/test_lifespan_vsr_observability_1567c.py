"""Bug #1567c: observability gap in the Bug #1567 startup orphan sweep.

The startup sweep for leaked `.versioned/{namespace}/v_*` snapshot
directories previously only logged when it found something (an `if
_vsr_result.scheduled_paths or _vsr_result.skipped_namespaces:` guard
around its INFO log), and the outer
`if global_lifecycle_manager is not None and snapshot_manager is not
None:` guard had no `else:` branch at all. Both make a sweep that ran and
found nothing indistinguishable from a sweep that silently never ran.

This module unit-tests two small, pure, module-level helpers extracted
from lifespan.py's Bug #1567 block --
`_log_vsr_sweep_completion(vsr_result, mode)` (unconditional completion
summary) and `_log_vsr_guard_skip(global_lifecycle_manager,
snapshot_manager)` (guard-skip WARNING) -- plus an AST-based source-text
wiring guard proving lifespan.py's real startup block actually calls them
in the right place (Messi Rule #12, anti-orphan-code; mirrors
test_lifespan_golden_repo_reconcile_wiring_bug1317.py's methodology, but
structural rather than indentation-based since this block sits deep
inside a large nested async function full of sibling `def`s).
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Optional, Sequence

from code_indexer.server.services.versioned_snapshot_reconciler import (
    VersionedSnapshotReconcileResult,
)
from code_indexer.server.startup.lifespan import (
    _log_vsr_guard_skip,
    _log_vsr_sweep_completion,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

_GUARD_LEFT = "global_lifecycle_manager"
_GUARD_RIGHT = "snapshot_manager"


def _lifespan_source() -> str:
    return _LIFESPAN_PATH.read_text()


def _call_name(node: ast.expr) -> Optional[str]:
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
    return None


def _contains_call_to(nodes: Sequence[ast.AST], name: str) -> bool:
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and _call_name(sub) == name:
                return True
    return False


def _is_name_is_not_none(value: ast.expr, expected_name: str) -> bool:
    """True iff *value* is structurally `<expected_name> is not None`."""
    return (
        isinstance(value, ast.Compare)
        and isinstance(value.left, ast.Name)
        and value.left.id == expected_name
        and len(value.ops) == 1
        and isinstance(value.ops[0], ast.IsNot)
        and len(value.comparators) == 1
        and isinstance(value.comparators[0], ast.Constant)
        and value.comparators[0].value is None
    )


def _find_vsr_guard_if(tree: ast.AST) -> Optional[ast.If]:
    """Find the specific `if global_lifecycle_manager is not None and
    snapshot_manager is not None:` statement that guards the Bug #1567
    versioned-snapshot orphan sweep -- via its structural shape (BoolOp/
    And of two `<name> is not None` Compare nodes over exactly the two
    expected names, each comparator a literal `None`), not string/
    indentation matching, AND confirmed to be the sweep's own guard by
    requiring its body to actually call reconcile_versioned_snapshots
    (so an unrelated guard sharing the same two variable names could
    never satisfy this test)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.BoolOp) or not isinstance(test.op, ast.And):
            continue
        if len(test.values) != 2:
            continue
        left_ok = any(_is_name_is_not_none(value, _GUARD_LEFT) for value in test.values)
        right_ok = any(
            _is_name_is_not_none(value, _GUARD_RIGHT) for value in test.values
        )
        if not (left_ok and right_ok):
            continue
        if not _contains_call_to(node.body, "reconcile_versioned_snapshots"):
            continue
        return node
    return None


class TestLogVsrSweepCompletionCleanAndAbortedRuns:
    def test_clean_zero_candidate_run_logs_one_info_and_zero_warnings(self, caplog):
        result = VersionedSnapshotReconcileResult(
            scanned_namespaces=["repo-a", "repo-b"],
            skipped_namespaces={},
            scheduled_paths=[],
            aborted=False,
        )
        with caplog.at_level(
            logging.DEBUG, logger="code_indexer.server.startup.lifespan"
        ):
            _log_vsr_sweep_completion(result)

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        warning_or_above = [r for r in caplog.records if r.levelno >= logging.WARNING]

        assert len(info_records) == 1, (
            "expected exactly one INFO completion record for a clean "
            f"zero-candidate sweep, got: {[r.message for r in caplog.records]}"
        )
        message = info_records[0].getMessage()
        assert "repo-a" in message and "repo-b" in message
        assert "0" in message  # 0 candidates found
        assert not warning_or_above, (
            "a clean zero-candidate completion must NEVER log at WARNING "
            f"or above (Bug #1565 lesson): {[r.message for r in warning_or_above]}"
        )

    def test_aborted_result_emits_no_completion_info_line(self, caplog):
        result = VersionedSnapshotReconcileResult(
            scanned_namespaces=[],
            skipped_namespaces={},
            scheduled_paths=[],
            aborted=True,
            abort_reason="single-flight conflict",
        )
        with caplog.at_level(
            logging.DEBUG, logger="code_indexer.server.startup.lifespan"
        ):
            _log_vsr_sweep_completion(result)

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert not info_records, (
            "an aborted sweep already logs its own message inside the "
            "reconciler -- the completion helper must not emit a second "
            f"INFO line: {[r.message for r in info_records]}"
        )


class TestLogVsrSweepCompletionUnconditionalDeletion:
    """Deletion is unconditional (Bug #1567: a fix must not ship behind
    an off-by-default toggle) -- deletions_scheduled now reflects purely
    whether the sweep found candidates, never a report/delete mode."""

    def test_candidates_found_reports_deletions_scheduled_true(self, caplog):
        result = VersionedSnapshotReconcileResult(
            scanned_namespaces=["repo-a", "repo-b", "repo-c"],
            skipped_namespaces={"repo-x": "some reason"},
            scheduled_paths=["/a/v_1", "/a/v_2"],
            aborted=False,
        )
        with caplog.at_level(
            logging.INFO, logger="code_indexer.server.startup.lifespan"
        ):
            _log_vsr_sweep_completion(result)

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info_records) == 1
        message = info_records[0].getMessage()
        assert "3" in message  # scanned namespace count
        assert "2" in message  # candidate count
        assert "1" in message  # skipped count
        assert "True" in message  # deletions were actually scheduled

    def test_zero_candidates_reports_deletions_scheduled_false(self, caplog):
        result = VersionedSnapshotReconcileResult(
            scanned_namespaces=["repo-a"],
            skipped_namespaces={},
            scheduled_paths=[],
            aborted=False,
        )
        with caplog.at_level(
            logging.INFO, logger="code_indexer.server.startup.lifespan"
        ):
            _log_vsr_sweep_completion(result)

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info_records) == 1
        message = info_records[0].getMessage()
        assert "False" in message, (
            f"zero candidates must report deletions_scheduled=False: {message}"
        )


class TestLogVsrGuardSkip:
    def test_missing_global_lifecycle_manager_names_it_and_not_the_other(self, caplog):
        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.startup.lifespan"
        ):
            _log_vsr_guard_skip(None, object())

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "global_lifecycle_manager" in message
        assert "snapshot_manager" not in message.replace("global_lifecycle_manager", "")

    def test_missing_snapshot_manager_names_it_and_not_the_other(self, caplog):
        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.startup.lifespan"
        ):
            _log_vsr_guard_skip(object(), None)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "snapshot_manager" in message
        assert "global_lifecycle_manager" not in message

    def test_missing_both_names_both(self, caplog):
        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.startup.lifespan"
        ):
            _log_vsr_guard_skip(None, None)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "global_lifecycle_manager" in message
        assert "snapshot_manager" in message


class TestLifespanVsrObservabilityWiringBug1567c:
    """AST-based structural guard: lifespan.py's real Bug #1567 block must
    call the unit-tested helpers above -- unconditionally inside the
    try-body, and from the guard's else-branch respectively. Unit-testing
    the helpers alone does not prove they are wired into the actual
    startup path."""

    def test_guard_if_node_is_found(self):
        tree = ast.parse(_lifespan_source())
        guard_if = _find_vsr_guard_if(tree)
        assert guard_if is not None, (
            "could not find the Bug #1567 sweep guard "
            "'if global_lifecycle_manager is not None and "
            "snapshot_manager is not None:' in lifespan.py -- has it been "
            "refactored away?"
        )

    def test_completion_helper_is_called_unconditionally_in_try_body(self):
        tree = ast.parse(_lifespan_source())
        guard_if = _find_vsr_guard_if(tree)
        assert guard_if is not None

        try_nodes = [n for n in guard_if.body if isinstance(n, ast.Try)]
        # Also catch a try nested one level deeper (e.g. inside another
        # try) via a full walk of the guard's body, in case of structural
        # drift -- but the DIRECT-statement requirement below still only
        # accepts a call sitting straight in some try.body, never nested
        # inside a further `if`.
        try_nodes += [
            n
            for stmt in guard_if.body
            for n in ast.walk(stmt)
            if isinstance(n, ast.Try) and n not in try_nodes
        ]
        assert try_nodes, "expected a try/except inside the guard's if-body"

        found_unconditional = False
        for try_node in try_nodes:
            for stmt in try_node.body:
                if isinstance(stmt, ast.Expr) and _call_name(stmt.value) == (
                    "_log_vsr_sweep_completion"
                ):
                    found_unconditional = True
        assert found_unconditional, (
            "_log_vsr_sweep_completion(...) must be called as a direct "
            "(unconditional) statement inside the sweep's try-body -- "
            "found it nested inside a conditional, or not found at all"
        )

    def test_guard_skip_helper_is_called_from_else_branch(self):
        tree = ast.parse(_lifespan_source())
        guard_if = _find_vsr_guard_if(tree)
        assert guard_if is not None
        assert guard_if.orelse, (
            "the Bug #1567 sweep guard has no 'else:' branch -- the "
            "guard-skip path is not wired"
        )
        assert _contains_call_to(guard_if.orelse, "_log_vsr_guard_skip"), (
            "'_log_vsr_guard_skip(' call not found inside the guard's 'else:' branch"
        )
