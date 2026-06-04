"""
Unit tests for Bug #1054: AnomalyAggregate unwrap in three Phase 3.7 handlers.

Tests assert that _audit_bidirectional_mismatch, _repair_self_loop, and
_repair_malformed_yaml each unwrap AnomalyAggregate.examples before delegating
to per-anomaly logic -- mirroring the working pattern in _repair_garbage_domain_rejected.

These are the three handlers that crashed with:
    'AnomalyAggregate' object has no attribute 'message'   (bidirectional)
    'AnomalyAggregate' object has no attribute 'file'      (self_loop / latent)
    (implicit attribute error in repair_single_malformed_yaml_anomaly)  (malformed_yaml / latent)
"""

import unittest
from pathlib import Path
from typing import List
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers: build a minimal DepMapRepairExecutor without touching real services
# ---------------------------------------------------------------------------


def _make_executor():
    """Return a DepMapRepairExecutor with just-enough no-op injections."""
    from code_indexer.server.services.dep_map_health_detector import (
        DepMapHealthDetector,
    )
    from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator
    from code_indexer.server.services.dep_map_repair_executor import (
        DepMapRepairExecutor,
    )

    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        domain_analyzer=None,
        invoke_llm_fn=None,
        repo_path_resolver=None,
    )


def _make_aggregate(anomaly_type_name: str, count: int = 3):
    """Build an AnomalyAggregate with *count* distinct AnomalyEntry examples."""
    from code_indexer.server.services.dep_map_parser_hygiene import (
        AnomalyAggregate,
        AnomalyEntry,
        AnomalyType,
    )

    atype = AnomalyType[anomaly_type_name]
    examples = [
        AnomalyEntry(
            type=atype,
            file=f"domain_{i}.md",
            message=f"example message {i}",
            channel="data",
        )
        for i in range(count)
    ]
    return AnomalyAggregate(type=atype, count=count, examples=examples)


# ---------------------------------------------------------------------------
# Test 1: _audit_bidirectional_mismatch unwraps AnomalyAggregate
# ---------------------------------------------------------------------------


class TestAuditBidirectionalMismatchAggregateUnwrap(unittest.TestCase):
    """Bug #1054 -- _audit_bidirectional_mismatch must unwrap AnomalyAggregate.

    Before fix: passes aggregate directly to audit_one_bidirectional_mismatch,
    which does anomaly.message -> AttributeError (aggregate has no .message).
    After fix: iterates examples, calls audit_one_bidirectional_mismatch once
    per AnomalyEntry.
    """

    def test_aggregate_dispatches_once_per_example(self):
        """AnomalyAggregate with 3 examples -> audit_one_bidirectional_mismatch called 3 times."""
        executor = _make_executor()
        aggregate = _make_aggregate("BIDIRECTIONAL_MISMATCH", count=3)

        fixed: List[str] = []
        errors: List[str] = []
        output_dir = Path("/tmp/bug1054_bidir_test")
        domains_json: List = []

        target = (
            "code_indexer.server.services.dep_map_repair_executor"
            ".audit_one_bidirectional_mismatch"
        )
        with patch(target) as mock_audit:
            mock_audit.return_value = None
            # Must NOT raise AttributeError
            executor._audit_bidirectional_mismatch(
                output_dir=output_dir,
                anomaly=aggregate,
                domains_json=domains_json,
                fixed=fixed,
                errors=errors,
                journal_disabled=True,
            )

        # Called exactly once per example, each with an AnomalyEntry (not aggregate)
        self.assertEqual(mock_audit.call_count, 3)
        from code_indexer.server.services.dep_map_parser_hygiene import AnomalyEntry

        for call_args in mock_audit.call_args_list:
            passed_anomaly = call_args.kwargs.get("anomaly") or call_args.args[1]
            self.assertIsInstance(
                passed_anomaly,
                AnomalyEntry,
                "Each call must receive an AnomalyEntry, not an AnomalyAggregate",
            )


# ---------------------------------------------------------------------------
# Test 2: _repair_self_loop unwraps AnomalyAggregate
# ---------------------------------------------------------------------------


class TestRepairSelfLoopAggregateUnwrap(unittest.TestCase):
    """Bug #1054 -- _repair_self_loop must unwrap AnomalyAggregate.

    Before fix: does anomaly.file directly on the aggregate -> AttributeError
    (AnomalyAggregate has no .file attribute).
    After fix: iterates examples, processes each AnomalyEntry individually.
    """

    def test_aggregate_dispatches_once_per_example(self):
        """AnomalyAggregate with 3 examples -> underlying repair called 3 times."""
        executor = _make_executor()
        aggregate = _make_aggregate("SELF_LOOP", count=3)

        fixed: List[str] = []
        errors: List[str] = []
        output_dir = Path("/tmp/bug1054_self_loop_test")

        # Patch the step functions that _repair_self_loop calls after reading anomaly.file
        resolve_target = (
            "code_indexer.server.services.dep_map_repair_executor"
            ".resolve_self_loop_md_path"
        )
        remove_target = (
            "code_indexer.server.services.dep_map_repair_executor.remove_self_loop_rows"
        )

        with patch(resolve_target, return_value=None) as mock_resolve:
            # return_value=None causes early return after path resolution,
            # preventing filesystem access while still exercising anomaly.file
            with patch(remove_target) as _mock_remove:
                # Must NOT raise AttributeError
                executor._repair_self_loop(
                    output_dir=output_dir,
                    anomaly=aggregate,
                    fixed=fixed,
                    errors=errors,
                )

        # resolve_self_loop_md_path called once per example (3 examples)
        self.assertEqual(
            mock_resolve.call_count,
            3,
            f"Expected 3 calls to resolve_self_loop_md_path, got {mock_resolve.call_count}",
        )


# ---------------------------------------------------------------------------
# Test 3: _repair_malformed_yaml unwraps AnomalyAggregate
# ---------------------------------------------------------------------------


class TestRepairMalformedYamlAggregateUnwrap(unittest.TestCase):
    """Bug #1054 -- _repair_malformed_yaml must unwrap AnomalyAggregate.

    Before fix: passes aggregate directly to repair_single_malformed_yaml_anomaly,
    which will fail on attribute access inside it.
    After fix: iterates examples, calls repair_single_malformed_yaml_anomaly once
    per AnomalyEntry.
    """

    def test_aggregate_dispatches_once_per_example(self):
        """AnomalyAggregate with 3 examples -> repair_single_malformed_yaml_anomaly called 3 times."""
        executor = _make_executor()
        aggregate = _make_aggregate("MALFORMED_YAML", count=3)

        fixed: List[str] = []
        errors: List[str] = []
        output_dir = Path("/tmp/bug1054_malformed_yaml_test")

        target = (
            "code_indexer.server.services.dep_map_repair_executor"
            ".repair_single_malformed_yaml_anomaly"
        )

        # Also need to patch _load_domains_json to avoid filesystem access
        with patch.object(executor, "_load_domains_json", return_value=[]):
            with patch(target) as mock_repair:
                mock_repair.return_value = None
                # Must NOT raise AttributeError
                executor._repair_malformed_yaml(
                    output_dir=output_dir,
                    anomaly=aggregate,
                    fixed=fixed,
                    errors=errors,
                )

        # Called exactly once per example, each with an AnomalyEntry (not aggregate)
        self.assertEqual(mock_repair.call_count, 3)
        from code_indexer.server.services.dep_map_parser_hygiene import AnomalyEntry

        for call_args in mock_repair.call_args_list:
            # repair_single_malformed_yaml_anomaly(output_dir, anomaly, ...)
            passed_anomaly = call_args.args[1] if len(call_args.args) > 1 else None
            if passed_anomaly is None:
                passed_anomaly = call_args.kwargs.get("anomaly")
            self.assertIsInstance(
                passed_anomaly,
                AnomalyEntry,
                "Each call must receive an AnomalyEntry, not an AnomalyAggregate",
            )


if __name__ == "__main__":
    unittest.main()
