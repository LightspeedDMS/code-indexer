"""Subprocess hardening tests for PythonEvaluatorSandbox."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch

import pytest


def _make_node():
    from code_indexer.xray.ast_engine import AstSearchEngine

    return AstSearchEngine().parse("x=1", "python")


def _run_evaluator_exits_11(
    code: str,
    node: Any,
    root: Any,
    source: str,
    lang: str,
    file_path: str,
    conn: Any,
) -> None:
    """Replacement for _run_evaluator that closes the pipe then calls os._exit(11)."""
    conn.close()
    os._exit(11)  # noqa: SLF001


class TestSubprocessHardening:
    def test_concurrent_attacks_no_canary_leak(self) -> None:
        from code_indexer.xray.sandbox import PythonEvaluatorSandbox

        sb = PythonEvaluatorSandbox()
        node = _make_node()
        canary = "/tmp/xray_hardening_canary_concurrent"
        if os.path.exists(canary):
            os.remove(canary)

        # All attack strings use dunder attributes in DUNDER_ATTR_BLOCKLIST,
        # so they are rejected at AST validation (failure="validation_failed").
        attacks = [
            "return ().__class__.__bases__[0].__subclasses__()",
            "return ''.join.__globals__",
            "return globals()['__import__']('os')",
        ] * 33
        safe = ["return True"] * 50
        codes = attacks + safe

        def run_one(code):
            return sb.run(
                code,
                node=node,
                root=node,
                source="x=1",
                lang="python",
                file_path="/tmp/x.py",
            )

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(run_one, codes))

        for code, r in zip(codes, results):
            if code == "return True":
                assert r.value is True
            else:
                assert r.failure is not None

        assert not os.path.exists(canary), "CANARY LEAKED under concurrent attack"

    def test_huge_literal_list_does_not_crash_parent(self) -> None:
        from code_indexer.xray.sandbox import PythonEvaluatorSandbox

        sb = PythonEvaluatorSandbox()
        node = _make_node()
        # [0] * 100000 uses BinOp which is not whitelisted; use list(range(N)) instead.
        result = sb.run(
            "return len(list(range(100000))) > 0",
            node=node,
            root=node,
            source="x=1",
            lang="python",
            file_path="/tmp/x.py",
        )
        assert result is not None

    def test_segfault_simulation_returns_subprocess_died(self) -> None:
        from code_indexer.xray.sandbox import PythonEvaluatorSandbox

        sb = PythonEvaluatorSandbox()
        node = _make_node()
        # Patch _run_evaluator at module level; the fork child resolves the
        # module-level name at call time so the replacement is picked up.
        # Must close conn before os._exit() so no pipe data is sent.
        with patch(
            "code_indexer.xray.sandbox._run_evaluator",
            side_effect=_run_evaluator_exits_11,
        ):
            result = sb.run(
                "return True",
                node=node,
                root=node,
                source="x=1",
                lang="python",
                file_path="/tmp/x.py",
            )
        assert result.failure == "evaluator_subprocess_died"

    @pytest.mark.skipif(sys.platform != "linux", reason="Linux-only psutil check")
    def test_no_zombie_after_attack_burst(self) -> None:
        from code_indexer.xray.sandbox import PythonEvaluatorSandbox

        sb = PythonEvaluatorSandbox()
        node = _make_node()
        # Attacks are rejected at validation (no subprocess spawned); mix in
        # real runs so the zombie check is meaningful.
        codes = [
            "return ().__class__.__bases__[0]",  # validation_failed
            "return True",
            "return ''.join.__globals__",  # validation_failed
            "return True",
        ] * 8
        for code in codes:
            sb.run(
                code,
                node=node,
                root=node,
                source="x=1",
                lang="python",
                file_path="/tmp/x.py",
            )
        try:
            import psutil

            kids = psutil.Process(os.getpid()).children(recursive=True)
            zombies = [k for k in kids if k.status() == psutil.STATUS_ZOMBIE]
            assert not zombies
        except ImportError:
            pytest.skip("psutil not available")
